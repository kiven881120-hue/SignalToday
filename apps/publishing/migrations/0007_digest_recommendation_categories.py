from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("publishing", "0006_digestitem_article_version")]

    operations = [
        migrations.AlterField(
            model_name="digestitem",
            name="category",
            field=models.CharField(
                choices=[
                    ("MUST_KNOW", "Must know"),
                    ("WORLD", "World"),
                    ("AI_TECH", "AI / Tech"),
                    ("FINANCE", "Finance"),
                    ("AUTO", "Auto"),
                    ("SCIENCE", "Science"),
                    ("EDUCATION", "Education"),
                    ("HEALTH", "Health"),
                    ("PERSONALIZED", "Personalized"),
                    ("DISCOVER", "Discover"),
                    ("GENERAL", "General"),
                ],
                max_length=20,
            ),
        ),
    ]
