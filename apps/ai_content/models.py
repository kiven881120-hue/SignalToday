from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.claims.models import ClaimType, EvidenceStance
from apps.claims.models import StrictImportanceIntegerField
from apps.jobs.models import JobRun, JobStep


class LLMPurpose(models.TextChoices):
    CLAIM_EXTRACTION = "CLAIM_EXTRACTION", "Claim extraction"
    SCORING_SUGGESTION = "SCORING_SUGGESTION", "Scoring suggestion"
    ARTICLE_GENERATION = "ARTICLE_GENERATION", "Article generation"
    ARTICLE_REVISION = "ARTICLE_REVISION", "Article revision"


class ModelCallStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    SUCCESS = "SUCCESS", "Succeeded"
    FAILED = "FAILED", "Failed"
    TIMEOUT = "TIMEOUT", "Timeout"
    RATE_LIMITED = "RATE_LIMITED", "Rate limited"
    PAUSED = "PAUSED", "Paused"
    PAUSED_BUDGET_EXCEEDED = "PAUSED_BUDGET_EXCEEDED", "Paused: budget exceeded"


class ReviewStatus(models.TextChoices):
    PROPOSED = "PROPOSED", "Proposed"
    ACCEPTED = "ACCEPTED", "Accepted"
    REJECTED = "REJECTED", "Rejected"


class EvidenceHintStatus(models.TextChoices):
    VALID = "VALID", "Valid"
    INVALID = "INVALID", "Invalid"
    SUPERSEDED = "SUPERSEDED", "Superseded"


class EvidenceHintInvalidReason(models.TextChoices):
    RAW_ITEM_NOT_IN_EVENT = "RAW_ITEM_NOT_IN_EVENT", "Raw item does not belong to Event"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND", "Quote was not found in source material"
    CRITICAL_TOKEN_MISMATCH = "CRITICAL_TOKEN_MISMATCH", "Critical Claim token is absent from quote"
    QUOTE_TOO_LONG = "QUOTE_TOO_LONG", "Quote exceeds the allowed length"


class PromptVersion(models.Model):
    purpose = models.CharField(max_length=32, choices=LLMPurpose.choices)
    version = models.CharField(max_length=50)
    system_prompt = models.TextField()
    output_schema_version = models.CharField(max_length=50)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["purpose", "version"], name="prompt_purpose_version_uniq")]

    def __str__(self):
        return f"{self.purpose}:{self.version}"

    def clean(self):
        super().clean()
        if self.pk and self.model_calls.exists():
            original = type(self).objects.filter(pk=self.pk).values(
                "purpose", "version", "system_prompt", "output_schema_version"
            ).first()
            current = {
                "purpose": self.purpose,
                "version": self.version,
                "system_prompt": self.system_prompt,
                "output_schema_version": self.output_schema_version,
            }
            if original and original != current:
                raise ValidationError("A PromptVersion used by ModelCall is immutable; create a new version.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class ModelPricing(models.Model):
    provider = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    input_price = models.DecimalField(max_digits=12, decimal_places=6)
    output_price = models.DecimalField(max_digits=12, decimal_places=6)
    currency = models.CharField(max_length=3, default="CNY")
    effective_from = models.DateTimeField()
    version = models.CharField(max_length=50)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "model", "version"], name="pricing_provider_model_version_uniq"),
            models.CheckConstraint(condition=Q(input_price__gte=0, output_price__gte=0), name="pricing_nonnegative"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.model}:{self.version}"


class ModelCall(models.Model):
    provider = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    purpose = models.CharField(max_length=100, choices=LLMPurpose.choices)
    prompt = models.ForeignKey(PromptVersion, on_delete=models.PROTECT, null=True, blank=True, related_name="model_calls")
    prompt_version = models.CharField(max_length=50)
    schema_version = models.CharField(max_length=50)
    analysis_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    request_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    total_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    pricing = models.ForeignKey(ModelPricing, on_delete=models.PROTECT, null=True, blank=True, related_name="model_calls")
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=32, choices=ModelCallStatus.choices, default=ModelCallStatus.PENDING, db_index=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(max_length=2000, blank=True)
    raw_output = models.TextField(max_length=20000, blank=True)
    job_run = models.ForeignKey(JobRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="model_calls")
    job_step = models.ForeignKey(JobStep, on_delete=models.SET_NULL, null=True, blank=True, related_name="model_calls")
    event = models.ForeignKey(
        "events.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="model_calls"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class ClaimCandidate(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="claim_candidates")
    model_call = models.ForeignKey(ModelCall, on_delete=models.PROTECT, related_name="claim_candidates")
    claim_text = models.CharField(max_length=500)
    normalized_claim = models.CharField(max_length=500, editable=False)
    claim_type = models.CharField(max_length=20, choices=ClaimType.choices)
    confidence = models.DecimalField(max_digits=5, decimal_places=4, validators=[MinValueValidator(0), MaxValueValidator(1)])
    evidence_hint = models.TextField(max_length=2000, blank=True, default="")
    importance_hint = StrictImportanceIntegerField(
        null=True, blank=True, default=None,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    importance_reason = models.CharField(max_length=500, null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True, default="")
    single_source_event = models.BooleanField(default=False)
    invalid_evidence_hint_count = models.PositiveIntegerField(default=0)
    raw_output = models.TextField(max_length=20000, blank=True)
    status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PROPOSED, db_index=True)
    review_reason = models.CharField(max_length=500, blank=True)
    accepted_claim = models.ForeignKey("claims.Claim", on_delete=models.SET_NULL, null=True, blank=True, related_name="accepted_candidates")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "normalized_claim"], name="candidate_event_normalized_uniq")]

    def clean(self):
        super().clean()
        if self.importance_hint is not None and not str(self.importance_reason or "").strip():
            raise ValidationError({"importance_reason": "A reason is required when importance_hint is set."})

    def save(self, *args, **kwargs):
        from apps.claims.normalization import normalize_claim_text

        self.normalized_claim = normalize_claim_text(self.claim_text)
        self.full_clean()
        super().save(*args, **kwargs)


class ClaimCandidateEvidenceHint(models.Model):
    candidate = models.ForeignKey(ClaimCandidate, on_delete=models.CASCADE, related_name="evidence_hints")
    raw_item = models.ForeignKey(
        "ingestion.RawItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="candidate_evidence_hints"
    )
    proposed_raw_item_id = models.PositiveBigIntegerField()
    quote = models.TextField(max_length=2000)
    normalized_quote = models.TextField(max_length=2000, editable=False)
    status = models.CharField(max_length=12, choices=EvidenceHintStatus.choices, db_index=True)
    is_current = models.BooleanField(default=False, db_index=True)
    invalid_reason = models.CharField(max_length=40, choices=EvidenceHintInvalidReason.choices, blank=True)
    superseded_by = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="supersedes"
    )
    hint_type = models.CharField(
        max_length=20,
        choices=EvidenceStance.choices,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["candidate"],
                condition=Q(status="VALID", is_current=True),
                name="candidate_current_valid_hint_uniq",
            ),
            models.CheckConstraint(
                condition=Q(is_current=False) | Q(status="VALID"),
                name="candidate_hint_current_is_valid",
            ),
            models.UniqueConstraint(
                fields=["candidate", "proposed_raw_item_id", "normalized_quote"],
                name="candidate_hint_source_quote_uniq",
            ),
        ]

    def save(self, *args, **kwargs):
        from apps.claims.normalization import normalize_evidence_text

        self.normalized_quote = normalize_evidence_text(self.quote)
        if self._state.adding and self.status == EvidenceHintStatus.VALID and not self.is_current:
            self.is_current = not type(self).objects.filter(
                candidate_id=self.candidate_id, status=EvidenceHintStatus.VALID, is_current=True
            ).exists()
        # PostgreSQL enforces the conditional current-hint uniqueness; Django's
        # Python validator cannot evaluate conditional constraints reliably.
        self.full_clean(validate_constraints=False)
        super().save(*args, **kwargs)


class ScoreSuggestion(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="score_suggestions")
    model_call = models.OneToOneField(ModelCall, on_delete=models.PROTECT, related_name="score_suggestion")
    importance_score = models.DecimalField(max_digits=5, decimal_places=3, validators=[MinValueValidator(0), MaxValueValidator(10)])
    credibility_score = models.DecimalField(max_digits=5, decimal_places=3, validators=[MinValueValidator(0), MaxValueValidator(10)])
    user_relevance_score = models.DecimalField(max_digits=5, decimal_places=3, validators=[MinValueValidator(0), MaxValueValidator(10)])
    information_gain_score = models.DecimalField(max_digits=5, decimal_places=3, validators=[MinValueValidator(0), MaxValueValidator(10)])
    freshness_score = models.DecimalField(max_digits=5, decimal_places=3, validators=[MinValueValidator(0), MaxValueValidator(10)])
    long_term_impact_score = models.DecimalField(max_digits=5, decimal_places=3, validators=[MinValueValidator(0), MaxValueValidator(10)])
    reasons = models.JSONField(default=dict)
    overall_reason = models.TextField(max_length=1000)
    status = models.CharField(max_length=20, choices=ReviewStatus.choices, default=ReviewStatus.PROPOSED, db_index=True)
    accepted_score = models.ForeignKey("scoring.Score", on_delete=models.SET_NULL, null=True, blank=True, related_name="accepted_suggestions")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
