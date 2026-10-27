from django.core.validators import MaxValueValidator, MinValueValidator
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Case, CharField, Count, F, Q, Value, When
from django.db.models.functions import Cast, Concat


class EventStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    RESOLVED = "RESOLVED", "Resolved"
    ARCHIVED = "ARCHIVED", "Archived"


class ClusterMethod(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    EXACT_TITLE = "EXACT_TITLE", "Exact title"
    HIGH_SIMILARITY = "HIGH_SIMILARITY", "High similarity"
    TOKEN_OVERLAP = "TOKEN_OVERLAP", "Token overlap"
    NEW_EVENT = "NEW_EVENT", "New event"


class Event(models.Model):
    title = models.CharField(max_length=500)
    normalized_title = models.CharField(max_length=500, blank=True, default="", db_index=True)
    cluster_key = models.CharField(max_length=64, blank=True, default="", db_index=True)
    cluster_method = models.CharField(max_length=24, choices=ClusterMethod.choices, blank=True, default="")
    cluster_confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    summary = models.TextField(blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_seen_at = models.DateTimeField(db_index=True)
    last_seen_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=20, choices=EventStatus.choices, default=EventStatus.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(cluster_confidence__isnull=True)
                | Q(cluster_confidence__gte=0, cluster_confidence__lte=1),
                name="event_cluster_confidence_range",
            )
        ]
        indexes = [
            models.Index(fields=["status", "last_seen_at"], name="event_status_lastseen_idx"),
            models.Index(fields=["first_seen_at", "last_seen_at"], name="event_seen_window_idx"),
            GinIndex(fields=["normalized_title"], name="event_title_trgm_idx", opclasses=["gin_trgm_ops"]),
        ]

    @property
    def raw_item_count(self):
        return self.raw_items.count()

    @property
    def unique_source_count(self):
        return self.raw_items.values("source_id").distinct().count()

    @property
    def source_count(self):
        return self.unique_source_count

    @property
    def independent_source_count(self):
        identity = Case(
            When(
                source__source_group="",
                then=Concat(Value("source:"), Cast("source_id", CharField())),
            ),
            default=Concat(Value("group:"), F("source__source_group")),
            output_field=CharField(),
        )
        return self.raw_items.aggregate(total=Count(identity, distinct=True))["total"]

    def save(self, *args, **kwargs):
        from .clustering import event_cluster_key, normalize_event_title

        self.normalized_title = normalize_event_title(self.title)
        self.cluster_key = event_cluster_key(self.normalized_title)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | {"normalized_title", "cluster_key"}
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title
