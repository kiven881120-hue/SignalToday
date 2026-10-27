import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0003_article_article_event_uniq")]

    operations = [
        migrations.AddField(
            model_name="article",
            name="published_version",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="published_for", to="publishing.articleversion",
            ),
        ),
        migrations.AlterField(
            model_name="article",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"), ("IN_REVIEW", "In review"),
                    ("APPROVED", "Approved"), ("PUBLISHED", "Published"),
                    ("CORRECTED", "Corrected"), ("RETRACTED", "Retracted"),
                ],
                db_index=True, default="DRAFT", max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="articleversion",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"), ("IN_REVIEW", "In review"),
                    ("APPROVED", "Approved"), ("PUBLISHED", "Published"),
                    ("CORRECTED", "Corrected"), ("RETRACTED", "Retracted"),
                ],
                db_index=True, default="DRAFT", max_length=20,
            ),
        ),
    ]
