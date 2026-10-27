from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from apps.ai_content.models import ModelCall
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.models import JobRun, JobStep
from apps.publishing.models import Article, ArticleVersion, ArticleVersionClaim, Image
from apps.reviews.models import Review
from apps.scoring.models import Score, ScoreRuleVersion
from apps.sources.models import Source


class CoreDomainModelTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.source = Source.objects.create(
            name="Fictional Observatory", source_type="OFFICIAL", url="https://example.test/source",
            domain="example.test", tier="A", reliability=0.95,
        )
        self.raw_item = RawItem.objects.create(
            source=self.source, external_id="item-1", url="https://example.test/item-1",
            title="A fictional item", discovered_at=now, published_at=now - timedelta(hours=1),
            content_hash="hash-1", raw_metadata={"kind": "test"},
        )
        self.event = Event.objects.create(
            title="A fictional event", first_seen_at=now, last_seen_at=now,
        )
        self.raw_item.event = self.event
        self.raw_item.save(update_fields=["event"])
        self.claim = Claim.objects.create(
            event=self.event, statement="A fictional fact.", normalized_statement="a fictional fact",
        )

    def test_source_and_raw_item_creation(self):
        self.assertEqual(self.raw_item.source, self.source)
        self.assertEqual(self.raw_item.raw_metadata["kind"], "test")
        self.assertIsNotNone(self.raw_item.discovered_at.tzinfo)

    def test_raw_item_source_external_id_is_unique(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RawItem.objects.create(source=self.source, external_id="item-1", url="https://example.test/other", title="Duplicate", discovered_at=timezone.now())
        RawItem.objects.create(source=self.source, external_id="", url="https://example.test/no-id-1", title="No ID 1", discovered_at=timezone.now())
        RawItem.objects.create(source=self.source, external_id="", url="https://example.test/no-id-2", title="No ID 2", discovered_at=timezone.now())

    def test_claim_evidence_links_claim_raw_item_and_source(self):
        evidence = ClaimEvidence.objects.create(
            claim=self.claim, raw_item=self.raw_item, source=self.source,
            evidence_type="PRIMARY", evidence_locator="p1", evidence_text="Minimal excerpt.",
            captured_at=timezone.now(),
        )
        self.assertEqual(evidence.claim.event, self.event)
        self.assertEqual(evidence.raw_item.source, self.source)

        other_source = Source.objects.create(name="Other", source_type="MEDIA", url="https://other.test", domain="other.test")
        evidence.source = other_source
        with self.assertRaises(ValidationError):
            evidence.full_clean()

    def test_article_version_and_claim_link_constraints(self):
        article = Article.objects.create(event=self.event)
        version = ArticleVersion.objects.create(
            article=article, version_number=1, title="Draft", one_sentence_summary="Summary",
            what_happened="What happened.",
        )
        article.current_version = version
        article.save(update_fields=["current_version", "updated_at"])
        ArticleVersionClaim.objects.create(article_version=version, claim=self.claim, paragraph_key="what", display_order=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArticleVersion.objects.create(article=article, version_number=1, title="Duplicate", one_sentence_summary="Summary", what_happened="Text")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArticleVersionClaim.objects.create(article_version=version, claim=self.claim, paragraph_key="why", display_order=2)
        self.assertEqual(article.current_version, version)

        other_event = Event.objects.create(
            title="Other article event", first_seen_at=timezone.now(), last_seen_at=timezone.now()
        )
        other_article = Article.objects.create(event=other_event)
        other_article.current_version = version
        with self.assertRaises(ValidationError):
            other_article.full_clean()

    def test_score_rule_version_and_target(self):
        rule = ScoreRuleVersion.objects.create(version="v1", name="Baseline")
        score = Score.objects.create(event=self.event, rule_version=rule, final_score=8.5, reasons={"note": "test"})
        self.assertEqual(score.rule_version, rule)
        with self.assertRaises(ValidationError):
            Score.objects.create(rule_version=rule, final_score=1)
        with self.assertRaises(ValidationError):
            Score.objects.create(event=self.event, article=Article.objects.create(event=self.event), rule_version=rule, final_score=1)

    def test_job_run_steps_model_call_and_audit(self):
        run = JobRun.objects.create(job_type="heartbeat", idempotency_key="run-1")
        step = JobStep.objects.create(job_run=run, step_name="connect", input_summary={"x": 1})
        call = ModelCall.objects.create(provider="fictional", model="none", purpose="test", prompt_version="1", schema_version="1", job_run=run)
        AuditLog.objects.create(action="create", object_type="JobRun", object_id=str(run.pk), after_data={"status": run.status})
        self.assertEqual(run.steps.get(), step)
        self.assertEqual(run.model_calls.get(), call)

    def test_review_and_image_relationships(self):
        article = Article.objects.create(event=self.event)
        version = ArticleVersion.objects.create(article=article, version_number=1, title="Draft", one_sentence_summary="Summary", what_happened="Text")
        review = Review.objects.create(article_version=version, reviewer=get_user_model().objects.create_user(username="reviewer"), decision="PENDING")
        image = Image.objects.create(article=article, article_version=version, image_type="hero", url="https://example.test/image.jpg", alt_text="Fictional image")
        self.assertEqual(review.article_version, version)
        self.assertEqual(image.article_version, version)
        other_event = Event.objects.create(
            title="Other image event", first_seen_at=timezone.now(), last_seen_at=timezone.now()
        )
        other_article = Article.objects.create(event=other_event)
        image.article = other_article
        with self.assertRaises(ValidationError):
            image.full_clean()

    def test_source_delete_is_protected_by_raw_item(self):
        with self.assertRaises(ProtectedError):
            self.source.delete()
