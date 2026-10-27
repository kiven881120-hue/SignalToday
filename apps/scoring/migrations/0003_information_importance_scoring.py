import django.core.validators
from decimal import Decimal
from django.db import migrations, models

import apps.scoring.rules


DEFAULT_CONFIG = {
    "weights": {
        "importance": "0.25",
        "credibility": "0.25",
        "user_relevance": "0.15",
        "information_gain": "0.15",
        "freshness": "0.10",
        "long_term_impact": "0.10",
    },
    "thresholds": {"FOCUS": "8.0", "NORMAL": "6.5", "CANDIDATE": "5.0"},
}


def initialize_scoring(apps, schema_editor):
    Rule = apps.get_model("scoring", "ScoreRuleVersion")
    Score = apps.get_model("scoring", "Score")
    Rule.objects.get_or_create(
        version="v1.0",
        defaults={
            "name": "default-information-value-v1",
            "description": "Deterministic six-dimension information importance scoring.",
            "rule_config": DEFAULT_CONFIG,
            "active": True,
        },
    )
    for score in Score.objects.all().iterator():
        value = score.final_score
        level = (
            "FOCUS" if value >= Decimal("8") else
            "NORMAL" if value >= Decimal("6.5") else
            "CANDIDATE" if value >= Decimal("5") else "REJECT"
        )
        Score.objects.filter(pk=score.pk).update(recommendation_level=level)


class Migration(migrations.Migration):
    dependencies = [("scoring", "0002_remove_score_score_has_target_and_more")]

    operations = [
        migrations.RenameField(model_name="score", old_name="impact", new_name="long_term_impact"),
        migrations.AlterField(
            model_name="scoreruleversion",
            name="rule_config",
            field=models.JSONField(default=apps.scoring.rules.default_rule_config),
        ),
        migrations.AddField(
            model_name="score",
            name="recommendation_level",
            field=models.CharField(
                choices=[("FOCUS", "Focus"), ("NORMAL", "Normal"), ("CANDIDATE", "Candidate"), ("REJECT", "Reject")],
                db_index=True,
                default="REJECT",
                max_length=20,
            ),
        ),
        *[
            migrations.AlterField(
                model_name="score",
                name=field,
                field=models.DecimalField(
                    decimal_places=3,
                    default=0,
                    max_digits=6,
                    validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10)],
                ),
            )
            for field in ("importance", "credibility", "user_relevance", "information_gain", "freshness", "long_term_impact")
        ],
        migrations.RunPython(initialize_scoring, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="score",
            constraint=models.UniqueConstraint(condition=models.Q(event__isnull=False), fields=("event", "rule_version"), name="score_event_rule_uniq"),
        ),
        migrations.AddConstraint(
            model_name="score",
            constraint=models.UniqueConstraint(condition=models.Q(article__isnull=False), fields=("article", "rule_version"), name="score_article_rule_uniq"),
        ),
        *[
            migrations.AddConstraint(
                model_name="score",
                constraint=models.CheckConstraint(
                    condition=models.Q((f"{field}__gte", 0), (f"{field}__lte", 10)),
                    name=f"score_{field}_range",
                ),
            )
            for field in ("importance", "credibility", "user_relevance", "information_gain", "freshness", "long_term_impact", "final_score")
        ],
    ]
