import json
import logging
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone

from apps.ai_content.costs import current_pricing, estimate_tokens, calculate_cost
from apps.ai_content.models import LLMPurpose, ModelCall, ModelCallStatus
from apps.ai_content.prompts import analysis_input_metrics, build_analysis_prompt, select_source_material
from apps.ai_content.providers.base import PermanentProviderError, ProviderError
from apps.ai_content.services import (
    AnalysisPaused,
    active_prompt,
    analyze_event_claims,
    configured_provider,
    suggest_event_score,
)
from apps.events.models import Event
from apps.jobs.contracts import JobStatus
from apps.jobs.models import JobRun, JobStep


PURPOSES = {"claims": LLMPurpose.CLAIM_EXTRACTION, "scoring": LLMPurpose.SCORING_SUGGESTION}
logger = logging.getLogger("signaltoday.llm")


class Command(BaseCommand):
    help = "Analyze Events with a structured LLM without creating Claims or final Scores."

    def add_arguments(self, parser):
        parser.add_argument("--event", type=int)
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--purpose", choices=PURPOSES, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        purpose = PURPOSES[options["purpose"]]
        events = Event.objects.filter(raw_items__isnull=False).distinct().order_by("pk")
        if options["event"] is not None:
            events = events.filter(pk=options["event"])
        events = events[: max(options["limit"], 0)]
        event_ids = list(events.values_list("pk", flat=True))
        prompt = active_prompt(purpose)
        if options["dry_run"]:
            for event in Event.objects.filter(pk__in=event_ids).order_by("pk"):
                system, user = build_analysis_prompt(event, prompt)
                pricing = current_pricing(settings.LLM_PROVIDER, settings.LLM_MODEL)
                estimated = calculate_cost(pricing, estimate_tokens(system + user), settings.LLM_MAX_OUTPUT_TOKENS) if pricing else None
                self.stdout.write(json.dumps({
                    "event_id": event.pk,
                    "purpose": options["purpose"],
                    "source_material_count": len(select_source_material(event, prompt)),
                    "analysis_inputs": analysis_input_metrics(event, prompt),
                    "input_characters": len(system + user),
                    "estimated_cost": str(estimated) if estimated is not None else None,
                    "would_call_provider": False,
                }, sort_keys=True))
            return

        job = JobRun.objects.create(job_type="LLM_EVENT_ANALYSIS", status=JobStatus.RUNNING, started_at=timezone.now())
        input_summary = {"event_count": len(event_ids), "purpose": purpose}
        if purpose == LLMPurpose.CLAIM_EXTRACTION:
            input_summary["analysis_inputs"] = {
                str(event.pk): analysis_input_metrics(event, prompt)
                for event in Event.objects.filter(pk__in=event_ids).order_by("pk")
            }
        step = JobStep.objects.create(
            job_run=job, step_name=f"analyze_{options['purpose']}", status=JobStatus.RUNNING,
            started_at=timezone.now(), input_summary=input_summary,
        )
        stats = {
            "event_count": len(event_ids), "processed": 0, "succeeded": 0, "success": 0,
            "failed": 0, "paused": 0, "candidates_created": 0, "claim_candidates": 0,
            "score_suggestions": 0, "invalid_hints": 0, "empty_claim_events": 0,
            "single_source_events": 0, "estimated_cost": "0", "actual_cost": "0",
            "content_filtered_events": 0,
        }
        try:
            provider = configured_provider()
        except PermanentProviderError as exc:
            stats["paused"] = len(event_ids)
            self._finish(job, step, stats, JobStatus.PAUSED, str(exc))
            self.stderr.write(str(exc))
            return
        for event in Event.objects.filter(pk__in=event_ids).order_by("pk"):
            stats["processed"] += 1
            if event.unique_source_count == 1:
                stats["single_source_events"] += 1
            try:
                if purpose == LLMPurpose.CLAIM_EXTRACTION:
                    results, _ = analyze_event_claims(event, provider=provider, job_run=job, job_step=step)
                    stats["claim_candidates"] += len(results)
                    stats["candidates_created"] += sum(bool(getattr(item, "_was_created", False)) for item in results)
                    stats["invalid_hints"] += sum(item.invalid_evidence_hint_count for item in results)
                    if not results:
                        stats["empty_claim_events"] += 1
                        if not ModelCall.objects.filter(job_run=job, event=event).exists():
                            stats["content_filtered_events"] += 1
                else:
                    suggest_event_score(event, provider=provider, job_run=job, job_step=step)
                    stats["score_suggestions"] += 1
                stats["success"] += 1
                stats["succeeded"] += 1
            except AnalysisPaused:
                stats["paused"] += 1
            except PermanentProviderError:
                stats["paused"] += 1
            except ProviderError:
                stats["failed"] += 1
            except Exception:
                stats["failed"] += 1
                logger.exception("Unexpected structured analysis failure event_id=%s", event.pk)
        calls = ModelCall.objects.filter(job_run=job)
        stats["estimated_cost"] = str(sum((call.estimated_cost or Decimal("0") for call in calls), Decimal("0")))
        stats["actual_cost"] = str(sum((call.actual_cost or Decimal("0") for call in calls), Decimal("0")))
        budget_paused = calls.filter(status=ModelCallStatus.PAUSED_BUDGET_EXCEEDED).exists()
        status = (
            JobStatus.FAILED if stats["failed"] else
            JobStatus.PAUSED_BUDGET_EXCEEDED if budget_paused else
            JobStatus.PAUSED if stats["paused"] else
            JobStatus.SUCCESS
        )
        self._finish(job, step, stats, status, "Analysis incomplete" if status != JobStatus.SUCCESS else "")
        self.stdout.write(json.dumps({"job_run_id": job.pk, "stats": stats}, sort_keys=True))

    @staticmethod
    def _finish(job, step, stats, status, error):
        now = timezone.now()
        step.status, step.finished_at, step.output_summary, step.error_message = status, now, stats, error
        step.save(update_fields=["status", "finished_at", "output_summary", "error_message"])
        job.status, job.finished_at, job.error_message = status, now, error
        job.save(update_fields=["status", "finished_at", "error_message"])
