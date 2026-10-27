from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("publishing", "0004_article_review_state")]

    operations = [
        migrations.CreateModel(
            name="Digest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("digest_date", models.DateField(unique=True)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("READY", "Ready")], db_index=True, default="DRAFT", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="DigestItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("category", models.CharField(choices=[("MUST_KNOW", "Must know"), ("WORLD", "World"), ("AI_TECH", "AI / Tech"), ("FINANCE", "Finance"), ("AUTO", "Auto"), ("SCIENCE", "Science"), ("EDUCATION", "Education"), ("HEALTH", "Health"), ("DISCOVER", "Discover")], max_length=20)),
                ("rank", models.PositiveIntegerField()),
                ("recommendation_reason", models.CharField(max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("article", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="digest_items", to="publishing.article")),
                ("digest", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="publishing.digest")),
            ],
            options={"ordering": ["digest", "rank"]},
        ),
        migrations.AddConstraint(
            model_name="digestitem",
            constraint=models.UniqueConstraint(fields=("digest", "article"), name="digest_article_uniq"),
        ),
        migrations.AddConstraint(
            model_name="digestitem",
            constraint=models.UniqueConstraint(fields=("digest", "rank"), name="digest_rank_uniq"),
        ),
    ]
