from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep
from apps.publishing.eligibility import ArticleGenerationEligibility, evaluate_article_generation_eligibility
from apps.scoring.models import Score, ScoreStatus
from apps.scoring.refresh import invalidate_event_score, refresh_event_score

from .evaluation import ClaimEvaluationService
from .evidence_review import review_claim_evidence
from .models import ClaimEvidence, EvidenceStance, VerificationStatus


PIPELINE_JOB_TYPE = "EVIDENCE_VERIFICATION_PIPELINE"
PIPELINE_STEPS = (
    "review_evidence",
    "evaluate_claim",
    "invalidate_score",
    "refresh_score",
    "check_eligibility",
)
REVIEW_REASON = "evidence verification pipeline"
INVALIDATION_REASON = "score refresh after claim/evidence verification"


class EvidenceVerificationPipelineError(Exception):
    pass


class EvidenceVerificationPipelineRetryRequired(EvidenceVerificationPipelineError):
    pass


class EvidenceVerificationPipelineRunning(EvidenceVerificationPipelineError):
    pass


@dataclass(frozen=True)
class EvidenceVerificationPipelineResult:
    job_run: JobRun
    evidence: ClaimEvidence
    eligibility: ArticleGenerationEligibility
    resumed: bool


def _complete_step(step, output):
    step.status = JobStatus.SUCCESS
    step.finished_at = timezone.now()
    step.output_summary = output
    step.error_message = ""
    step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])


def _start_step(step):
    step.status = JobStatus.RUNNING
    step.started_at = timezone.now()
    step.finished_at = None
    step.error_message = ""
    step.save(update_fields=["status", "started_at", "finished_at", "error_message"])


def _fail(run, step, exc):
    now = timezone.now()
    error = str(exc)[:2000]
    step.status = JobStatus.FAILED
    step.finished_at = now
    step.error_message = error
    step.save(update_fields=["status", "finished_at", "error_message"])
    run.status = JobStatus.FAILED
    run.finished_at = now
    run.error_message = error
    run.save(update_fields=["status", "finished_at", "error_message"])


def _acquire_pipeline(evidence, retry):
    key = f"evidence-verification:{evidence.pk}"
    with transaction.atomic():
        run, created = JobRun.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "job_type": PIPELINE_JOB_TYPE,
                "status": JobStatus.RUNNING,
                "started_at": timezone.now(),
            },
        )
        run = JobRun.objects.select_for_update().get(pk=run.pk)
        if not created:
            if run.status == JobStatus.SUCCESS:
                return run, False, True
            if run.status == JobStatus.RUNNING:
                raise EvidenceVerificationPipelineRunning("Pipeline is already running")
            if run.status == JobStatus.FAILED and not retry:
                raise EvidenceVerificationPipelineRetryRequired(
                    "Pipeline failed previously; pass --retry to continue"
                )
            if run.status != JobStatus.FAILED:
                raise EvidenceVerificationPipelineError(
                    f"Pipeline cannot resume from status {run.status}"
                )
            run.status = JobStatus.RUNNING
            run.started_at = timezone.now()
            run.finished_at = None
            run.error_message = ""
            run.save(update_fields=["status", "started_at", "finished_at", "error_message"])
        for name in PIPELINE_STEPS:
            JobStep.objects.get_or_create(
                job_run=run,
                step_name=name,
                defaults={
                    "status": JobStatus.PENDING,
                    "input_summary": {"evidence_id": evidence.pk},
                },
            )
        return run, not created, False


def _step(run, name):
    return run.steps.get(step_name=name)


def _current_score(evidence):
    return (
        Score.objects.filter(
            event_id=evidence.claim.event_id,
            rule_version__active=True,
        )
        .select_related("rule_version")
        .order_by("-rule_version__created_at", "-rule_version_id")
        .first()
    )


def verify_evidence_pipeline(evidence_id, *, retry=False):
    evidence = ClaimEvidence.objects.select_related("claim__event").filter(pk=evidence_id).first()
    if evidence is None:
        raise EvidenceVerificationPipelineError("ClaimEvidence does not exist")
    if evidence.stance not in {EvidenceStance.MENTIONS, EvidenceStance.SUPPORTS}:
        raise EvidenceVerificationPipelineError("Evidence must be MENTIONS or SUPPORTS")

    run, resumed, already_successful = _acquire_pipeline(evidence, retry)
    if already_successful:
        evidence.refresh_from_db()
        return EvidenceVerificationPipelineResult(
            run,
            evidence,
            evaluate_article_generation_eligibility(evidence.claim.event_id),
            resumed=False,
        )

    active_step = None
    try:
        review_step = _step(run, "review_evidence")
        if review_step.status != JobStatus.SUCCESS:
            active_step = review_step
            _start_step(review_step)
            evidence.refresh_from_db()
            if evidence.stance == EvidenceStance.MENTIONS:
                review = review_claim_evidence(
                    evidence.pk,
                    new_stance=EvidenceStance.SUPPORTS,
                    reason=REVIEW_REASON,
                )
                review_changed = review.changed
                evidence = review.evidence
            elif evidence.stance == EvidenceStance.SUPPORTS:
                review_changed = resumed
            else:
                raise EvidenceVerificationPipelineError("Evidence is no longer reviewable")
            _complete_step(review_step, {
                "evidence_id": evidence.pk,
                "claim_id": evidence.claim_id,
                "event_id": evidence.claim.event_id,
                "changed": review_changed,
                "stance": evidence.stance,
            })
        else:
            review_changed = bool(review_step.output_summary.get("changed"))

        evaluation_step = _step(run, "evaluate_claim")
        if evaluation_step.status != JobStatus.SUCCESS:
            active_step = evaluation_step
            _start_step(evaluation_step)
            evidence.refresh_from_db()
            claim = evidence.claim
            score = _current_score(evidence)
            settled = (
                claim.verification_status == VerificationStatus.SUPPORTED
                and score is not None
                and score.status == ScoreStatus.CALCULATED
                and not review_changed
            )
            old_status = claim.verification_status
            evaluation_run_id = None
            if settled:
                new_status = old_status
            else:
                stats, results, evaluation_run = ClaimEvaluationService().run(
                    claim.__class__.objects.filter(pk=claim.pk)
                )
                if evaluation_run.status != JobStatus.SUCCESS or stats["errors"]:
                    raise EvidenceVerificationPipelineError("Claim Evaluation failed")
                new_status = results[0][1].status
                evaluation_run_id = evaluation_run.pk
            status_changed = old_status != new_status
            _complete_step(evaluation_step, {
                "claim_id": claim.pk,
                "event_id": claim.event_id,
                "old_status": old_status,
                "new_status": new_status,
                "changed": status_changed,
                "skipped": settled,
                "evaluation_job_run_id": evaluation_run_id,
            })
        else:
            status_changed = bool(evaluation_step.output_summary.get("changed"))

        score = _current_score(evidence)
        refresh_needed = review_changed or status_changed or (
            score is not None and score.status == ScoreStatus.INVALID
        )

        invalidate_step = _step(run, "invalidate_score")
        if invalidate_step.status != JobStatus.SUCCESS:
            active_step = invalidate_step
            _start_step(invalidate_step)
            if refresh_needed:
                invalidation = invalidate_event_score(
                    evidence.claim.event_id,
                    INVALIDATION_REASON,
                )
                score_id = invalidation.score.pk if invalidation.score else None
                reason_code = invalidation.reason_code
                changed = invalidation.changed
            else:
                score_id = score.pk if score else None
                reason_code = "SCORE_REFRESH_NOT_REQUIRED"
                changed = False
            _complete_step(invalidate_step, {
                "event_id": evidence.claim.event_id,
                "score_id": score_id,
                "changed": changed,
                "reason_code": reason_code,
            })

        refresh_step = _step(run, "refresh_score")
        if refresh_step.status != JobStatus.SUCCESS:
            active_step = refresh_step
            _start_step(refresh_step)
            score = _current_score(evidence)
            should_refresh = refresh_needed or (
                score is not None and score.status == ScoreStatus.INVALID
            )
            if should_refresh:
                refresh = refresh_event_score(evidence.claim.event_id)
                output = {
                    "event_id": evidence.claim.event_id,
                    "score_id": refresh.score.pk,
                    "changed": refresh.changed,
                    "status": refresh.score.status,
                    "refresh_job_run_id": refresh.job_run.pk,
                }
            else:
                output = {
                    "event_id": evidence.claim.event_id,
                    "score_id": score.pk if score else None,
                    "changed": False,
                    "status": score.status if score else None,
                    "skipped": True,
                }
            _complete_step(refresh_step, output)

        eligibility_step = _step(run, "check_eligibility")
        if eligibility_step.status != JobStatus.SUCCESS:
            active_step = eligibility_step
            _start_step(eligibility_step)
            eligibility = evaluate_article_generation_eligibility(evidence.claim.event_id)
            _complete_step(eligibility_step, {
                "event_id": evidence.claim.event_id,
                "eligible": eligibility.eligible,
                "reason_code": eligibility.reason_code,
                "reason": eligibility.reason,
                "eligible_claim_ids": list(eligibility.eligible_claim_ids),
                "score_id": eligibility.score_id,
            })
        else:
            eligibility = evaluate_article_generation_eligibility(evidence.claim.event_id)

        run.status = JobStatus.SUCCESS
        run.finished_at = timezone.now()
        run.error_message = ""
        run.save(update_fields=["status", "finished_at", "error_message"])
        evidence.refresh_from_db()
        return EvidenceVerificationPipelineResult(run, evidence, eligibility, resumed)
    except Exception as exc:
        if active_step is not None:
            _fail(run, active_step, exc)
        if isinstance(exc, EvidenceVerificationPipelineError):
            raise
        raise EvidenceVerificationPipelineError(str(exc)) from exc
