from django.db import models
from django.db.models import Q
from django.core.validators import MaxValueValidator, MinValueValidator


class SourceType(models.TextChoices):
    OFFICIAL = "OFFICIAL", "Official"
    MEDIA = "MEDIA", "Media"
    INDUSTRY = "INDUSTRY", "Industry"
    COMMUNITY = "COMMUNITY", "Community"
    SEARCH = "SEARCH", "Search"


class SourceTier(models.TextChoices):
    A = "A", "A"
    B = "B", "B"
    C = "C", "C"
    D = "D", "D"
    E = "E", "E"


class Source(models.Model):
    identifier = models.SlugField(max_length=100, unique=True, null=True, blank=True)
    name = models.CharField(max_length=200)
    source_type = models.CharField(max_length=20, choices=SourceType.choices)
    url = models.URLField(unique=True)
    domain = models.CharField(max_length=255, db_index=True)
    organization = models.CharField(max_length=200, blank=True)
    source_group = models.CharField(max_length=200, blank=True, default="", db_index=True)
    country = models.CharField(max_length=2, blank=True)
    language = models.CharField(max_length=16, default="en")
    tier = models.CharField(max_length=1, choices=SourceTier.choices, default=SourceTier.C)
    reliability = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    active = models.BooleanField(default=True, db_index=True)
    crawl_policy = models.JSONField(default=dict, blank=True)
    license_state = models.CharField(max_length=32, default="UNKNOWN")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(reliability__gte=0, reliability__lte=1),
                name="source_reliability_range",
            )
        ]
        indexes = [models.Index(fields=["active", "tier"], name="source_active_tier_idx")]

    def __str__(self):
        return self.name
