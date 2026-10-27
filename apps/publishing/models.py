from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError

from apps.events.models import Event


class ArticleStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    IN_REVIEW = "IN_REVIEW", "In review"
    APPROVED = "APPROVED", "Approved"
    PUBLISHED = "PUBLISHED", "Published"
    CORRECTED = "CORRECTED", "Corrected"
    RETRACTED = "RETRACTED", "Retracted"


class RiskLevel(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


class ArticleClassification(models.TextChoices):
    MUST_KNOW = "MUST_KNOW", "Must know"
    DISCOVER = "DISCOVER", "Discover"


class Article(models.Model):
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="articles")
    status = models.CharField(max_length=20, choices=ArticleStatus.choices, default=ArticleStatus.DRAFT, db_index=True)
    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices, default=RiskLevel.LOW, db_index=True)
    current_version = models.ForeignKey("ArticleVersion", on_delete=models.SET_NULL, null=True, blank=True, related_name="current_for")
    published_version = models.ForeignKey(
        "ArticleVersion", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="published_for",
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    corrected_at = models.DateTimeField(null=True, blank=True)
    retracted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event"], name="article_event_uniq")]

    def clean(self):
        super().clean()
        if self.current_version_id and self.current_version.article_id != self.pk:
            raise ValidationError({"current_version": "Current version must belong to this article."})
        if self.published_version_id and self.published_version.article_id != self.pk:
            raise ValidationError({"published_version": "Published version must belong to this article."})


class ArticleVersion(models.Model):
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField()
    title = models.CharField(max_length=500)
    one_sentence_summary = models.TextField()
    what_happened = models.TextField()
    why_it_matters = models.TextField(blank=True)
    who_is_affected = models.TextField(blank=True)
    example = models.TextField(blank=True)
    source_note = models.TextField(blank=True)
    language = models.CharField(max_length=16, default="en")
    status = models.CharField(max_length=20, choices=ArticleStatus.choices, default=ArticleStatus.DRAFT, db_index=True)
    classification = models.CharField(
        max_length=20, choices=ArticleClassification.choices, blank=True, default="", db_index=True
    )
    classified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="classified_article_versions",
    )
    classified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="article_versions")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["article", "version_number"], name="article_version_number_uniq")]
        ordering = ["article", "version_number"]

    CONTENT_FIELDS = (
        "title", "one_sentence_summary", "what_happened", "why_it_matters",
        "who_is_affected", "example", "source_note", "language",
    )
    CLASSIFICATION_FIELDS = ("classification", "classified_by_id", "classified_at")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "status", *self.CONTENT_FIELDS, *self.CLASSIFICATION_FIELDS
            ).first()
            immutable = {
                ArticleStatus.APPROVED, ArticleStatus.PUBLISHED,
                ArticleStatus.CORRECTED, ArticleStatus.RETRACTED,
            }
            if original and original["status"] in immutable:
                if any(original[field] != getattr(self, field) for field in self.CONTENT_FIELDS):
                    raise ValidationError("Approved or published ArticleVersion content is immutable; create a revision.")
                if any(
                    original[field] != getattr(self, field)
                    for field in self.CLASSIFICATION_FIELDS
                ):
                    raise ValidationError(
                        "Approved or published ArticleVersion classification is immutable."
                    )
        super().save(*args, **kwargs)


class ArticleVersionClaim(models.Model):
    article_version = models.ForeignKey(ArticleVersion, on_delete=models.CASCADE, related_name="claim_links")
    claim = models.ForeignKey("claims.Claim", on_delete=models.PROTECT, related_name="article_links")
    paragraph_key = models.CharField(max_length=100)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["article_version", "claim"], name="article_version_claim_uniq")]
        ordering = ["article_version", "display_order"]

    def clean(self):
        super().clean()
        if self.article_version_id and self.claim_id:
            if self.article_version.article.event_id != self.claim.event_id:
                raise ValidationError({"claim": "Claim must belong to the Article Event."})
            if self.article_version.status != ArticleStatus.DRAFT:
                raise ValidationError("Claim links on a submitted ArticleVersion are immutable.")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.select_related("article_version").filter(pk=self.pk).first()
            if original and original.article_version.status != ArticleStatus.DRAFT:
                raise ValidationError("Claim links on a submitted ArticleVersion are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.article_version.status != ArticleStatus.DRAFT:
            raise ValidationError("Claim links on a submitted ArticleVersion are immutable.")
        return super().delete(*args, **kwargs)


class ArticleContentValidation(models.Model):
    article_version = models.OneToOneField(
        ArticleVersion, on_delete=models.CASCADE, related_name="content_validation"
    )
    content_hash = models.CharField(max_length=64)
    grounding_status = models.CharField(max_length=32, db_index=True)
    grounding_validator_version = models.CharField(max_length=50)
    grounding_result = models.JSONField(default=dict)
    quality_status = models.CharField(max_length=32, db_index=True)
    quality_validator_version = models.CharField(max_length=50)
    quality_input = models.JSONField(default=dict)
    quality_result = models.JSONField(default=dict)
    content_quality = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    validated_at = models.DateTimeField(auto_now=True)


class DigestStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    READY = "READY", "Ready"


class DigestCategory(models.TextChoices):
    MUST_KNOW = "MUST_KNOW", "Must know"
    WORLD = "WORLD", "World"
    AI_TECH = "AI_TECH", "AI / Tech"
    FINANCE = "FINANCE", "Finance"
    AUTO = "AUTO", "Auto"
    SCIENCE = "SCIENCE", "Science"
    EDUCATION = "EDUCATION", "Education"
    HEALTH = "HEALTH", "Health"
    PERSONALIZED = "PERSONALIZED", "Personalized"
    DISCOVER = "DISCOVER", "Discover"
    GENERAL = "GENERAL", "General"


class Digest(models.Model):
    digest_date = models.DateField(unique=True)
    status = models.CharField(max_length=20, choices=DigestStatus.choices, default=DigestStatus.DRAFT, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class DigestItem(models.Model):
    digest = models.ForeignKey(Digest, on_delete=models.CASCADE, related_name="items")
    article = models.ForeignKey(Article, on_delete=models.PROTECT, related_name="digest_items")
    article_version = models.ForeignKey(
        ArticleVersion, on_delete=models.PROTECT, related_name="digest_items"
    )
    category = models.CharField(max_length=20, choices=DigestCategory.choices)
    rank = models.PositiveIntegerField()
    recommendation_reason = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["digest", "article"], name="digest_article_uniq"),
            models.UniqueConstraint(fields=["digest", "rank"], name="digest_rank_uniq"),
        ]
        indexes = [
            models.Index(
                fields=["article_version", "digest"],
                name="digestitem_version_digest_idx",
            ),
        ]
        ordering = ["digest", "rank"]

    def clean(self):
        super().clean()
        if not self.article_version_id:
            raise ValidationError({"article_version": "A fixed ArticleVersion is required."})
        if self.article_id and self.article_version.article_id != self.article_id:
            raise ValidationError(
                {"article_version": "ArticleVersion must belong to the DigestItem Article."}
            )
        if self._state.adding:
            article = self.article
            if (
                self.article_version.status != ArticleStatus.PUBLISHED
                or article.status != ArticleStatus.PUBLISHED
                or article.current_version_id != self.article_version_id
                or article.published_version_id != self.article_version_id
            ):
                raise ValidationError({
                    "article_version": (
                        "Only the Article's current formal PUBLISHED ArticleVersion "
                        "may enter a Digest."
                    )
                })
        if self.pk:
            original_version_id = type(self).objects.filter(pk=self.pk).values_list(
                "article_version_id", flat=True
            ).first()
            if original_version_id and original_version_id != self.article_version_id:
                raise ValidationError(
                    {"article_version": "DigestItem ArticleVersion is immutable."}
                )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class ImageLicenseState(models.TextChoices):
    UNKNOWN = "UNKNOWN", "Unknown"
    PENDING = "PENDING", "Pending"
    PERMITTED = "PERMITTED", "Permitted"
    RESTRICTED = "RESTRICTED", "Restricted"
    EXPIRED = "EXPIRED", "Expired"


class ImageStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    READY = "READY", "Ready"
    FAILED = "FAILED", "Failed"


class Image(models.Model):
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="images")
    article_version = models.ForeignKey(ArticleVersion, on_delete=models.CASCADE, null=True, blank=True, related_name="images")
    image_type = models.CharField(max_length=30)
    status = models.CharField(max_length=20, choices=ImageStatus.choices, default=ImageStatus.PENDING, db_index=True)
    url = models.URLField(max_length=2048)
    source = models.CharField(max_length=200, blank=True)
    license_state = models.CharField(max_length=20, choices=ImageLicenseState.choices, default=ImageLicenseState.UNKNOWN, db_index=True)
    license_note = models.TextField(blank=True)
    alt_text = models.CharField(max_length=500, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    model = models.CharField(max_length=100, blank=True)
    prompt_hash = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if self.article_version_id and self.article_version.article_id != self.article_id:
            raise ValidationError({"article_version": "Article version must belong to this article."})
