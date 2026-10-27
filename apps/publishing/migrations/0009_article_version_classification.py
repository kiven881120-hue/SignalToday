import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0008_article_content_validation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="articleversion",
            name="classification",
            field=models.CharField(
                blank=True,
                choices=[("MUST_KNOW", "Must know"), ("DISCOVER", "Discover")],
                db_index=True,
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="articleversion",
            name="classified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="articleversion",
            name="classified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="classified_article_versions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
