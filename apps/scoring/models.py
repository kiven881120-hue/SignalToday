from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator

from apps.events.models import Event

from .rules import DIMENSIONS, default_rule_config, recommendation_level, validate_rule_config, weighted_score


class ScoreStatus(models.TextChoices):
    CALCULATED = "CALCULATED", "Calculated"
    INVALID = "INVALID", "Invalid"


class RecommendationLevel(models.TextChoices):
    FOCUS = "FOCUS", "Focus"
    NORMAL = "NORMAL", "Normal"
    CANDIDATE = "CANDIDATE", "Candidate"
    REJECT = "REJECT", "Reject"


class ScoreRuleVersion(models.Model):
    version = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    rule_config = models.JSONField(default=default_rule_config)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.version

    def clean(self):
        super().clean()
        validate_rule_config(self.rule_config)
        if self.pk and self.scores.exists():
            original = type(self).objects.filter(pk=self.pk).values("version", "rule_config").first()
            if original and (original["version"] != self.version or original["rule_config"] != self.rule_config):
                raise ValidationError("A scoring rule with recorded Scores is immutable; create a new version.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Score(models.Model):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, null=True, blank=True, related_name="scores")
    article = models.ForeignKey("publishing.Article", on_delete=models.CASCADE, null=True, blank=True, related_name="scores")
    importance = models.DecimalField(max_digits=6, decimal_places=3, default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    credibility = models.DecimalField(max_digits=6, decimal_places=3, default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    user_relevance = models.DecimalField(max_digits=6, decimal_places=3, default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    information_gain = models.DecimalField(max_digits=6, decimal_places=3, default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    freshness = models.DecimalField(max_digits=6, decimal_places=3, default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    long_term_impact = models.DecimalField(max_digits=6, decimal_places=3, default=0, validators=[MinValueValidator(0), MaxValueValidator(10)])
    penalty = models.DecimalField(max_digits=6, decimal_places=3, default=0)
    final_score = models.DecimalField(max_digits=8, decimal_places=3)
    recommendation_level = models.CharField(max_length=20, choices=RecommendationLevel.choices, default=RecommendationLevel.REJECT, db_index=True)
    rule_version = models.ForeignKey(ScoreRuleVersion, on_delete=models.PROTECT, related_name="scores")
    reasons = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=ScoreStatus.choices, default=ScoreStatus.CALCULATED, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(event__isnull=False, article__isnull=True) | Q(event__isnull=True, article__isnull=False)),
                name="score_has_one_target",
            ),
            models.UniqueConstraint(fields=["event", "rule_version"], condition=Q(event__isnull=False), name="score_event_rule_uniq"),
            models.UniqueConstraint(fields=["article", "rule_version"], condition=Q(article__isnull=False), name="score_article_rule_uniq"),
            *[
                models.CheckConstraint(condition=Q(**{f"{field}__gte": 0, f"{field}__lte": 10}), name=f"score_{field}_range")
                for field in (*DIMENSIONS, "final_score")
            ],
        ]
        indexes = [models.Index(fields=["rule_version", "created_at"], name="score_rule_created_idx")]

    def clean(self):
        super().clean()
        if bool(self.event_id) == bool(self.article_id):
            raise ValidationError("Score must have exactly one Event or Article target.")

    def save(self, *args, **kwargs):
        values = {field: getattr(self, field) for field in DIMENSIONS}
        self.final_score = weighted_score(values, self.rule_version.rule_config)
        self.recommendation_level = recommendation_level(self.final_score, self.rule_version.rule_config)
        self.full_clean()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"final_score", "recommendation_level"}
        super().save(*args, **kwargs)
