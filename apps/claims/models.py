from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Q

from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.sources.models import Source


class StrictImportanceIntegerField(models.PositiveSmallIntegerField):
    """An integer field that refuses bools, decimals, floats, and numeric strings."""

    def to_python(self, value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("Importance must be an integer between 0 and 100.")
        return super().to_python(value)


class ClaimType(models.TextChoices):
    FACT = "FACT", "Fact"
    DATE = "DATE", "Date"
    NUMBER = "NUMBER", "Number"
    QUOTE = "QUOTE", "Quote"
    LOCATION = "LOCATION", "Location"
    PERSON = "PERSON", "Person"
    ORGANIZATION = "ORGANIZATION", "Organization"
    PRODUCT = "PRODUCT", "Product"
    IMPACT = "IMPACT", "Impact"


class VerificationStatus(models.TextChoices):
    UNVERIFIED = "UNVERIFIED", "Unverified"
    SUPPORTED = "SUPPORTED", "Supported"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED", "Partially supported"
    CONTRADICTED = "CONTRADICTED", "Contradicted"
    CONFLICTED = "CONFLICTED", "Conflicted"
    REJECTED = "REJECTED", "Rejected"
    NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"


class EvidenceType(models.TextChoices):
    OFFICIAL = "OFFICIAL", "Official"
    NEWS_REPORT = "NEWS_REPORT", "News report"
    INDUSTRY_MEDIA = "INDUSTRY_MEDIA", "Industry media"
    SOCIAL_LEAD = "SOCIAL_LEAD", "Social lead"
    PRIMARY = "PRIMARY", "Primary"
    SECONDARY = "SECONDARY", "Secondary"
    CONTEXT = "CONTEXT", "Context"


class EvidenceStance(models.TextChoices):
    SUPPORTS = "SUPPORTS", "Supports"
    CONTRADICTS = "CONTRADICTS", "Contradicts"
    MENTIONS = "MENTIONS", "Mentions"
    UNCLEAR = "UNCLEAR", "Unclear"


class Claim(models.Model):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="claims")
    claim_type = models.CharField(max_length=20, choices=ClaimType.choices, default=ClaimType.FACT)
    statement = models.TextField()
    normalized_statement = models.TextField(blank=True, editable=False)
    importance = StrictImportanceIntegerField(
        null=True,
        blank=True,
        default=None,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    verification_status = models.CharField(max_length=20, choices=VerificationStatus.choices, default=VerificationStatus.UNVERIFIED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(importance__gte=0, importance__lte=100),
                name="claim_importance_range",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_statement=""),
                name="claim_normalized_not_empty",
            ),
            models.UniqueConstraint(
                fields=["event", "normalized_statement"],
                name="claim_event_normalized_uniq",
            ),
        ]
        indexes = [models.Index(fields=["event", "verification_status"], name="claim_event_status_idx")]

    def __str__(self):
        return self.statement

    def clean(self):
        super().clean()
        from .normalization import normalize_claim_text

        self.normalized_statement = normalize_claim_text(self.statement)
        if not self.normalized_statement:
            raise ValidationError({"statement": "Claim text cannot be empty."})
        if self.importance is not None and self.importance > 0 and self.verification_status != VerificationStatus.SUPPORTED:
            raise ValidationError({"importance": "Nonzero importance requires a SUPPORTED Claim."})

    def save(self, *args, **kwargs):
        from .normalization import normalize_claim_text

        self.normalized_statement = normalize_claim_text(self.statement)
        self.full_clean()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"normalized_statement"}
        super().save(*args, **kwargs)


class ClaimEvidence(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="evidence")
    raw_item = models.ForeignKey(RawItem, on_delete=models.PROTECT, related_name="claim_evidence")
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="claim_evidence")
    evidence_type = models.CharField(max_length=20, choices=EvidenceType.choices, blank=True, default="")
    stance = models.CharField(max_length=20, choices=EvidenceStance.choices, default=EvidenceStance.UNCLEAR)
    evidence_locator = models.CharField(max_length=1000, blank=True, default="")
    evidence_text = models.TextField(max_length=2000)
    normalized_evidence_text = models.TextField(max_length=2000, blank=True, editable=False)
    captured_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(normalized_evidence_text=""),
                name="evidence_normalized_not_empty",
            ),
            models.UniqueConstraint(
                fields=["claim", "raw_item", "normalized_evidence_text"],
                name="evidence_claim_raw_text_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["claim", "captured_at"], name="evidence_claim_captured_idx"),
            models.Index(fields=["claim", "stance"], name="evidence_claim_stance_idx"),
        ]

    def clean(self):
        super().clean()
        from .normalization import normalize_evidence_text

        self.normalized_evidence_text = normalize_evidence_text(self.evidence_text)
        if not self.normalized_evidence_text:
            raise ValidationError({"evidence_text": "Evidence text cannot be empty."})
        if len(self.evidence_text or "") > 2000:
            raise ValidationError({"evidence_text": "Evidence text cannot exceed 2000 characters."})
        if self.raw_item_id and not self.raw_item.event_id:
            raise ValidationError({"raw_item": "RawItem must belong to an Event before evidence is created."})
        if self.claim_id and self.raw_item_id and self.claim.event_id != self.raw_item.event_id:
            raise ValidationError({"raw_item": "Claim 与 Evidence 的 RawItem 必须属于同一个 Event。"})
        if self.raw_item_id and self.source_id and self.source_id != self.raw_item.source_id:
            raise ValidationError({"source": "Source must match the raw item's source."})

    def save(self, *args, **kwargs):
        from .normalization import normalize_evidence_text

        self.normalized_evidence_text = normalize_evidence_text(self.evidence_text)
        if self.raw_item_id and not self.source_id:
            self.source_id = self.raw_item.source_id
        if not self.evidence_type and self.raw_item_id:
            self.evidence_type = evidence_type_for_tier(self.raw_item.source.tier)
        self.full_clean()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                "source", "evidence_type", "normalized_evidence_text"
            }
        super().save(*args, **kwargs)


def evidence_type_for_tier(tier):
    return {
        "A": EvidenceType.OFFICIAL,
        "B": EvidenceType.NEWS_REPORT,
        "C": EvidenceType.INDUSTRY_MEDIA,
        "D": EvidenceType.SOCIAL_LEAD,
        "E": EvidenceType.SECONDARY,
    }.get(tier, EvidenceType.SECONDARY)
