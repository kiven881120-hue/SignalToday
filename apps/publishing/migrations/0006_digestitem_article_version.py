import django.db.models.deletion
from django.db import migrations, models


def require_empty_digest_items(apps, schema_editor):
    DigestItem = apps.get_model("publishing", "DigestItem")
    if DigestItem.objects.exists():
        raise RuntimeError(
            "DigestItem rows must be migrated explicitly before adding version snapshots."
        )


class Migration(migrations.Migration):
    dependencies = [("publishing", "0005_digest")]

    operations = [
        migrations.AddField(
            model_name="digestitem",
            name="article_version",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="digest_items",
                to="publishing.articleversion",
            ),
        ),
        migrations.RunPython(require_empty_digest_items, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="digestitem",
            name="article_version",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="digest_items",
                to="publishing.articleversion",
            ),
        ),
        migrations.AddIndex(
            model_name="digestitem",
            index=models.Index(
                fields=["article_version", "digest"],
                name="digestitem_version_digest_idx",
            ),
        ),
    ]
