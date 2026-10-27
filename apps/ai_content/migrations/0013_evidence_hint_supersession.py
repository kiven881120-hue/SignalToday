from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


def select_existing_current_hints(apps, schema_editor):
    Hint = apps.get_model("ai_content", "ClaimCandidateEvidenceHint")
    candidate_ids = Hint.objects.filter(status="VALID").values_list("candidate_id", flat=True).distinct()
    for candidate_id in candidate_ids.iterator():
        hint = Hint.objects.filter(candidate_id=candidate_id, status="VALID").order_by("id").first()
        Hint.objects.filter(pk=hint.pk).update(is_current=True)


def clear_existing_current_hints(apps, schema_editor):
    apps.get_model("ai_content", "ClaimCandidateEvidenceHint").objects.update(is_current=False)


class Migration(migrations.Migration):
    dependencies = [("ai_content", "0012_article_generation_v1_3_gate")]

    operations = [
        migrations.AlterField(
            model_name="claimcandidateevidencehint", name="status",
            field=models.CharField(choices=[("VALID", "Valid"), ("INVALID", "Invalid"), ("SUPERSEDED", "Superseded")], db_index=True, max_length=12),
        ),
        migrations.AddField(
            model_name="claimcandidateevidencehint", name="is_current",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="claimcandidateevidencehint", name="superseded_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supersedes", to="ai_content.claimcandidateevidencehint"),
        ),
        migrations.RunPython(select_existing_current_hints, clear_existing_current_hints),
        migrations.AddConstraint(
            model_name="claimcandidateevidencehint",
            constraint=models.UniqueConstraint(condition=Q(status="VALID", is_current=True), fields=("candidate",), name="candidate_current_valid_hint_uniq"),
        ),
        migrations.AddConstraint(
            model_name="claimcandidateevidencehint",
            constraint=models.CheckConstraint(condition=Q(is_current=False) | Q(status="VALID"), name="candidate_hint_current_is_valid"),
        ),
    ]
