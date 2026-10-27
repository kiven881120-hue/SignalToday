from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ingestion", "0004_rawitem_event_rawitem_rawitem_event_published_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="rawitem",
            name="content_updated_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
