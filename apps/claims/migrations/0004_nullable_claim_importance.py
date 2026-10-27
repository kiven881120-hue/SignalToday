from django.db import migrations, models
import django.core.validators
import apps.claims.models


def default_zero_to_unassessed(apps, schema_editor):
    Claim = apps.get_model("claims", "Claim")
    Claim.objects.filter(importance=0).update(importance=None)


def unassessed_to_default_zero(apps, schema_editor):
    Claim = apps.get_model("claims", "Claim")
    Claim.objects.filter(importance__isnull=True).update(importance=0)


class Migration(migrations.Migration):
    dependencies = [("claims", "0003_claim_evidence_validation")]

    operations = [
        migrations.AlterField(
            model_name="claim",
            name="importance",
            field=apps.claims.models.StrictImportanceIntegerField(
                blank=True,
                default=None,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(100),
                ],
            ),
        ),
        migrations.RunPython(default_zero_to_unassessed, unassessed_to_default_zero),
    ]
