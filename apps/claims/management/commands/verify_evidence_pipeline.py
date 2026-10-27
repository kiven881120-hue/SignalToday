import json

from django.core.management.base import BaseCommand, CommandError

from apps.claims.verification_pipeline import (
    EvidenceVerificationPipelineError,
    verify_evidence_pipeline,
)


class Command(BaseCommand):
    help = "Run the single-Evidence verification pipeline."

    def add_arguments(self, parser):
        parser.add_argument("--evidence", type=int, required=True)
        parser.add_argument("--retry", action="store_true")

    def handle(self, *args, **options):
        try:
            result = verify_evidence_pipeline(options["evidence"], retry=options["retry"])
        except EvidenceVerificationPipelineError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "job_run_id": result.job_run.pk,
            "status": result.job_run.status,
            "evidence_id": result.evidence.pk,
            "evidence_stance": result.evidence.stance,
            "claim_id": result.evidence.claim_id,
            "resumed": result.resumed,
            "eligible": result.eligibility.eligible,
            "reason_code": result.eligibility.reason_code,
        }, sort_keys=True))
