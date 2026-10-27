from django.db import models
from django.db.models import Q

from apps.sources.models import Source


class RawItemStatus(models.TextChoices):
    DISCOVERED = "DISCOVERED", "Discovered"
    PROCESSED = "PROCESSED", "Processed"
    IGNORED = "IGNORED", "Ignored"
    FAILED = "FAILED", "Failed"


class RawItem(models.Model):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="raw_items")
    event = models.ForeignKey(
        "events.Event",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="raw_items",
    )
    external_id = models.CharField(max_length=512, blank=True)
    url = models.URLField(max_length=2048)
    canonical_url = models.URLField(max_length=2048, blank=True)
    title = models.CharField(max_length=500)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    content_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    discovered_at = models.DateTimeField(db_index=True)
    content_hash = models.CharField(max_length=128, blank=True, db_index=True)
    raw_metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=RawItemStatus.choices, default=RawItemStatus.DISCOVERED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=~Q(external_id=""),
                name="rawitem_source_external_id_uniq",
            ),
            models.UniqueConstraint(
                fields=["source", "canonical_url"],
                condition=~Q(canonical_url=""),
                name="rawitem_source_canonical_uniq",
            ),
            models.UniqueConstraint(
                fields=["source", "content_hash"],
                condition=~Q(content_hash=""),
                name="rawitem_source_hash_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "discovered_at"], name="rawitem_source_discovered_idx"),
            models.Index(fields=["event", "published_at"], name="rawitem_event_published_idx"),
        ]

    def __str__(self):
        return self.title
