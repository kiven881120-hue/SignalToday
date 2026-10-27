import html
import unicodedata

from django.db import migrations, models


def normalize(value, *, casefold=False):
    result = " ".join(unicodedata.normalize("NFKC", html.unescape(value or "")).split()).strip()
    return result.casefold() if casefold else result


def populate_normalized_text(apps, schema_editor):
    Claim = apps.get_model("claims", "Claim")
    ClaimEvidence = apps.get_model("claims", "ClaimEvidence")
    for claim in Claim.objects.all().iterator():
        normalized = normalize(claim.statement, casefold=True)
        if not normalized:
            raise RuntimeError(f"Claim {claim.pk} has empty text and must be corrected before migration")
        Claim.objects.filter(pk=claim.pk).update(normalized_statement=normalized)
    for evidence in ClaimEvidence.objects.all().iterator():
        normalized = normalize(evidence.evidence_text)
        if not normalized:
            raise RuntimeError(f"ClaimEvidence {evidence.pk} has empty text and must be corrected before migration")
        ClaimEvidence.objects.filter(pk=evidence.pk).update(normalized_evidence_text=normalized)


class Migration(migrations.Migration):
    dependencies = [
        ("claims", "0002_claim_claim_importance_range"),
        ("ingestion", "0004_rawitem_event_rawitem_rawitem_event_published_idx"),
    ]

    operations = [
        migrations.AlterField(
            model_name="claim",
            name="claim_type",
            field=models.CharField(
                choices=[
                    ("FACT", "Fact"), ("DATE", "Date"), ("NUMBER", "Number"), ("QUOTE", "Quote"),
                    ("LOCATION", "Location"), ("PERSON", "Person"), ("ORGANIZATION", "Organization"),
                    ("PRODUCT", "Product"), ("IMPACT", "Impact"),
                ],
                default="FACT",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="claim",
            name="verification_status",
            field=models.CharField(
                choices=[
                    ("UNVERIFIED", "Unverified"), ("SUPPORTED", "Supported"),
                    ("PARTIALLY_SUPPORTED", "Partially supported"), ("CONTRADICTED", "Contradicted"),
                    ("CONFLICTED", "Conflicted"), ("REJECTED", "Rejected"), ("NEEDS_REVIEW", "Needs review"),
                ],
                db_index=True,
                default="UNVERIFIED",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="claim",
            name="normalized_statement",
            field=models.TextField(blank=True, editable=False),
        ),
        migrations.RenameField(model_name="claimevidence", old_name="evidence_excerpt", new_name="evidence_text"),
        migrations.RenameField(model_name="claimevidence", old_name="retrieved_at", new_name="captured_at"),
        migrations.RemoveConstraint(model_name="claimevidence", name="claim_evidence_locator_uniq"),
        migrations.RemoveIndex(model_name="claimevidence", name="evidence_claim_retrieved_idx"),
        migrations.AlterField(
            model_name="claimevidence",
            name="evidence_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("OFFICIAL", "Official"), ("NEWS_REPORT", "News report"),
                    ("INDUSTRY_MEDIA", "Industry media"), ("SOCIAL_LEAD", "Social lead"),
                    ("PRIMARY", "Primary"), ("SECONDARY", "Secondary"), ("CONTEXT", "Context"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="claimevidence",
            name="evidence_locator",
            field=models.CharField(blank=True, default="", max_length=1000),
        ),
        migrations.AlterField(
            model_name="claimevidence",
            name="evidence_text",
            field=models.TextField(max_length=2000),
        ),
        migrations.AddField(
            model_name="claimevidence",
            name="normalized_evidence_text",
            field=models.TextField(blank=True, default="", editable=False, max_length=2000),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="claimevidence",
            name="stance",
            field=models.CharField(
                choices=[("SUPPORTS", "Supports"), ("CONTRADICTS", "Contradicts"), ("MENTIONS", "Mentions"), ("UNCLEAR", "Unclear")],
                default="UNCLEAR",
                max_length=20,
            ),
        ),
        migrations.RunPython(populate_normalized_text, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="claim",
            constraint=models.CheckConstraint(condition=~models.Q(normalized_statement=""), name="claim_normalized_not_empty"),
        ),
        migrations.AddConstraint(
            model_name="claim",
            constraint=models.UniqueConstraint(fields=("event", "normalized_statement"), name="claim_event_normalized_uniq"),
        ),
        migrations.AddConstraint(
            model_name="claimevidence",
            constraint=models.CheckConstraint(condition=~models.Q(normalized_evidence_text=""), name="evidence_normalized_not_empty"),
        ),
        migrations.AddConstraint(
            model_name="claimevidence",
            constraint=models.UniqueConstraint(
                fields=("claim", "raw_item", "normalized_evidence_text"), name="evidence_claim_raw_text_uniq"
            ),
        ),
        migrations.AddIndex(
            model_name="claimevidence",
            index=models.Index(fields=["claim", "captured_at"], name="evidence_claim_captured_idx"),
        ),
        migrations.AddIndex(
            model_name="claimevidence",
            index=models.Index(fields=["claim", "stance"], name="evidence_claim_stance_idx"),
        ),
    ]
