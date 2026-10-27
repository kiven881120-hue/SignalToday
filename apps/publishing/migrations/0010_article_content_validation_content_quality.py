from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0009_article_version_classification")]

    operations = [
        migrations.AddField(
            model_name="articlecontentvalidation",
            name="content_quality",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
