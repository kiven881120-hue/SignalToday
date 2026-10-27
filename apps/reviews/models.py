from django.conf import settings
from django.db import models

from apps.publishing.models import ArticleVersion, RiskLevel


class ReviewDecision(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    NEEDS_EDIT = "NEEDS_EDIT", "Needs edit"


class Review(models.Model):
    article_version = models.ForeignKey(ArticleVersion, on_delete=models.CASCADE, related_name="reviews")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviews")
    risk_level = models.CharField(max_length=10, choices=RiskLevel.choices, default=RiskLevel.LOW)
    decision = models.CharField(max_length=20, choices=ReviewDecision.choices, default=ReviewDecision.PENDING, db_index=True)
    comment = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
