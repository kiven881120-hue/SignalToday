import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0007_digest_recommendation_categories")]

    operations = [
        migrations.CreateModel(
            name="ArticleContentValidation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content_hash", models.CharField(max_length=64)),
                ("grounding_status", models.CharField(db_index=True, max_length=32)),
                ("grounding_validator_version", models.CharField(max_length=50)),
                ("grounding_result", models.JSONField(default=dict)),
                ("quality_status", models.CharField(db_index=True, max_length=32)),
                ("quality_validator_version", models.CharField(max_length=50)),
                ("quality_input", models.JSONField(default=dict)),
                ("quality_result", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("validated_at", models.DateTimeField(auto_now=True)),
                ("article_version", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="content_validation", to="publishing.articleversion")),
            ],
        ),
    ]
