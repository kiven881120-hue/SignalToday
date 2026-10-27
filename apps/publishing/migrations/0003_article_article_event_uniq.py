from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0004_event_event_title_trgm_idx"),
        ("publishing", "0002_image_status"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="article",
            constraint=models.UniqueConstraint(fields=("event",), name="article_event_uniq"),
        ),
    ]
