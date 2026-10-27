import json
import threading
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.ai_content.models import ModelCall
from apps.audit.models import AuditLog
from apps.claims.models import Claim, ClaimEvidence, EvidenceStance, VerificationStatus
from apps.events.models import Event
from apps.ingestion.models import RawItem
from apps.jobs.models import JobRun
from apps.publishing import brief
from apps.publishing.brief import (
    AUDIT_ACTION,
    DOWNGRADE_REASON,
    ArticleBriefDowngradeError,
    downgrade_article_to_brief,
)
from apps.publishing.models import (
    Article,
    ArticleStatus,
    ArticleVersion,
    ArticleVersionClaim,
)
from apps.reviews.models import Review
from apps.scoring.models import Score, ScoreRuleVersion
from apps.sources.models import Source


class BriefDowngradeFixtureMixin:
    def make_fixture(self, key="brief"):
        now = timezone.now()
        actor = get_user_model().objects.create_user(username=f"operator-{key}")
        event = Event.objects.create(
            title="Anak Krakatau Rumbles Again",
            first_seen_at=now,
            last_seen_at=now,
        )
        source = Source.objects.create(
            identifier=f"nasa-{key}",
            name="NASA",
            source_type="OFFICIAL",
            url=f"https://{key}.invalid",
            domain=f"{key}.invalid",
            tier="A",
        )
        evidence_text = (
            "A powerful eruption from the Indonesian volcano lofted ash into the atmosphere, "
            "disrupting thousands of flights and degrading air quality."
        )
        raw = RawItem.objects.create(
            source=source,
            event=event,
            external_id=key,
            url=f"{source.url}/item",
            title=event.title,
            raw_metadata={"summary": evidence_text},
            discovered_at=now,
        )
        claim = Claim.objects.create(
            event=event,
            claim_type="IMPACT",
            statement=(
                "The eruption disrupted thousands of flights and degraded air quality."
            ),
            verification_status=VerificationStatus.SUPPORTED,
            importance=74,
        )
        evidence = ClaimEvidence.objects.create(
            claim=claim,
            raw_item=raw,
            source=source,
            evidence_text=evidence_text,
            stance=EvidenceStance.SUPPORTS,
            captured_at=now,
        )
        rule = ScoreRuleVersion.objects.filter(active=True).first()
        score = Score.objects.create(
            event=event,
            rule_version=rule,
            importance=Decimal("7.400"),
            credibility=Decimal("10.000"),
            user_relevance=Decimal("7.400"),
            information_gain=Decimal("5.000"),
            freshness=Decimal("7.000"),
            long_term_impact=Decimal("7.400"),
            final_score=Decimal("0.000"),
        )
        article = Article.objects.create(event=event, status=ArticleStatus.DRAFT)
        versions = []
        for number in (1, 2, 3):
            versions.append(ArticleVersion.objects.create(
                article=article,
                version_number=number,
                title=(
                    "印尼火山喷发导致数千航班受影响、空气质量下降"
                ),
                one_sentence_summary=(
                    "NASA观测信息显示，一场强烈的印尼火山喷发将火山灰送入大气层，"
                    "造成数千架次航班中断，并导致空气质量下降。"
                ),
                what_happened=(
                    "根据NASA提供的观测信息，一场来自印度尼西亚火山的"
                    "强烈喷发将火山灰抛入大气层。火山灰扩散后对空中交通"
                    "造成大范围干扰，数千架次航班因此中断；同时，火山灰"
                    "和喷发物也使空气质量变差。"
                ),
                why_it_matters=(
                    "该事件值得关注，因为NASA观测信息显示，一场强烈火山"
                    "喷发将火山灰送入大气层，导致数千架次航班中断并造成"
                    "空气质量下降。"
                ),
                who_is_affected=(
                    "已核实证据未具体确定受影响的人群或机构；其仅表明"
                    "火山喷发导致数千架次航班中断，并造成空气质量下降。"
                ),
                example=(
                    "例如，NASA观测信息显示，这场来自印度尼西亚火山的"
                    "强烈喷发将火山灰送入大气层，导致数千架次航班中断"
                    "并造成空气质量下降。"
                ),
                source_note=f"NASA: {raw.url}",
                language="zh-CN",
                status=ArticleStatus.DRAFT,
                created_by=actor if number > 1 else None,
            ))
        article.current_version = versions[-1]
        article.save(update_fields=["current_version", "updated_at"])
        ArticleVersionClaim.objects.create(
            article_version=versions[-1],
            claim=claim,
            paragraph_key="article",
            display_order=1,
        )
        return actor, article, versions, claim, evidence, score

    def controlled(self, actor, article, versions, *, dry_run=False):
        with (
            patch("apps.publishing.brief.CONTROLLED_ARTICLE_ID", article.pk),
            patch("apps.publishing.brief.CONTROLLED_SOURCE_VERSION_ID", versions[2].pk),
            patch(
                "apps.publishing.brief.CONTROLLED_SOURCE_CONTENT_HASH",
                brief._content_hash(versions[2]),
            ),
        ):
            return downgrade_article_to_brief(
                actor=actor,
                source_version_id=versions[2].pk,
                dry_run=dry_run,
            )


class ArticleBriefDowngradeTests(BriefDowngradeFixtureMixin, TestCase):
    def setUp(self):
        self.actor, self.article, self.versions, self.claim, self.evidence, self.score = (
            self.make_fixture()
        )

    def test_a_to_g_creates_minimal_brief_with_unchanged_core_fields(self):
        source = self.versions[2]
        result = self.controlled(self.actor, self.article, self.versions)
        target = result.target_version
        self.assertEqual(target.version_number, 4)
        self.assertEqual(target.title, source.title)
        self.assertEqual(target.one_sentence_summary, source.one_sentence_summary)
        self.assertEqual(target.what_happened, source.what_happened)
        self.assertEqual((target.why_it_matters, target.who_is_affected, target.example), ("", "", ""))
        self.assertEqual(result.article_mode.value, "BRIEF")
        self.assertEqual(result.why_status.value, "WHY_NONE_ESTABLISHED")
        self.assertEqual(result.who_status.value, "WHO_NONE_ESTABLISHED")
        self.assertEqual(result.example_status.value, "EXAMPLE_NONE")

    def test_h_to_k_claim_evidence_score_and_version_three_are_unchanged(self):
        source = self.versions[2]
        content_before = {field: getattr(source, field) for field in ArticleVersion.CONTENT_FIELDS}
        claim_before = (self.claim.statement, self.claim.verification_status, self.claim.importance)
        evidence_before = (self.evidence.evidence_text, self.evidence.stance, self.evidence.raw_item_id)
        score_before = tuple(Score.objects.filter(pk=self.score.pk).values_list(
            "importance", "credibility", "user_relevance", "information_gain",
            "freshness", "long_term_impact", "final_score", "recommendation_level",
        ).get())
        result = self.controlled(self.actor, self.article, self.versions)
        source.refresh_from_db()
        self.claim.refresh_from_db()
        self.evidence.refresh_from_db()
        self.assertEqual(content_before, {
            field: getattr(source, field) for field in ArticleVersion.CONTENT_FIELDS
        })
        self.assertEqual(claim_before, (
            self.claim.statement, self.claim.verification_status, self.claim.importance
        ))
        self.assertEqual(evidence_before, (
            self.evidence.evidence_text, self.evidence.stance, self.evidence.raw_item_id
        ))
        self.assertEqual(score_before, tuple(Score.objects.filter(pk=self.score.pk).values_list(
            "importance", "credibility", "user_relevance", "information_gain",
            "freshness", "long_term_impact", "final_score", "recommendation_level",
        ).get()))
        self.assertEqual(
            list(result.target_version.claim_links.values_list(
                "claim_id", "paragraph_key", "display_order"
            )),
            list(source.claim_links.values_list("claim_id", "paragraph_key", "display_order")),
        )

    def test_l_to_n_all_required_validators_pass(self):
        result = self.controlled(self.actor, self.article, self.versions)
        self.assertEqual(result.grounding.status.value, "PASS")
        self.assertEqual(result.fact_drift.status.value, "PASS")
        self.assertEqual(result.quality.quality_status.value, "PASS")
        self.assertEqual(result.quality.quality_level.value, "LEVEL_2")
        self.assertTrue(result.schema_passed)
        self.assertTrue(result.ready_for_article_review)

    def test_p_repeated_downgrade_is_idempotent(self):
        first = self.controlled(self.actor, self.article, self.versions)
        second = self.controlled(self.actor, self.article, self.versions)
        self.assertEqual(second.target_version.pk, first.target_version.pk)
        self.assertTrue(second.already_applied)
        self.assertEqual(self.article.versions.count(), 4)
        self.assertEqual(AuditLog.objects.filter(action=AUDIT_ACTION).count(), 1)
        self.assertEqual(JobRun.objects.filter(job_type="ARTICLE_BRIEF_DOWNGRADE").count(), 1)

    def test_q_wrong_article_is_blocked(self):
        with self.assertRaises(ArticleBriefDowngradeError) as caught:
            downgrade_article_to_brief(actor=self.actor, article_id=self.article.pk + 1)
        self.assertEqual(caught.exception.code, "ARTICLE_BRIEF_DOWNGRADE_WRONG_ARTICLE")

    def test_r_wrong_version_is_blocked(self):
        with self.assertRaises(ArticleBriefDowngradeError) as caught:
            downgrade_article_to_brief(
                actor=self.actor, source_version_id=self.versions[1].pk
            )
        self.assertEqual(caught.exception.code, "ARTICLE_BRIEF_DOWNGRADE_WRONG_VERSION")

    def test_s_published_source_is_blocked(self):
        self.article.status = ArticleStatus.PUBLISHED
        self.article.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ArticleBriefDowngradeError):
            self.controlled(self.actor, self.article, self.versions)
        self.assertEqual(self.article.versions.count(), 3)

    def test_t_and_u_no_llm_or_model_call(self):
        before = ModelCall.objects.count()
        self.controlled(self.actor, self.article, self.versions)
        self.assertEqual(ModelCall.objects.count(), before)

    def test_v_audit_is_minimal_and_correct(self):
        result = self.controlled(self.actor, self.article, self.versions)
        audit = AuditLog.objects.get(action=AUDIT_ACTION)
        self.assertEqual(audit.actor_id, self.actor.pk)
        self.assertEqual(audit.before_data, {
            "article_id": self.article.pk,
            "source_version_id": self.versions[2].pk,
        })
        self.assertEqual(audit.after_data["target_version_id"], result.target_version.pk)
        self.assertEqual(audit.after_data["job_run_id"], result.job_run.pk)
        self.assertEqual(audit.after_data["reason"], DOWNGRADE_REASON)
        self.assertNotIn("body", audit.after_data)
        self.assertNotIn("evidence", audit.after_data)
        self.assertNotIn("prompt", audit.after_data)

    def test_w_current_transition_and_draft_states_are_correct(self):
        result = self.controlled(self.actor, self.article, self.versions)
        self.article.refresh_from_db()
        self.versions[2].refresh_from_db()
        self.assertEqual(self.article.current_version_id, result.target_version.pk)
        self.assertEqual(self.article.status, ArticleStatus.DRAFT)
        self.assertEqual(self.versions[2].status, ArticleStatus.DRAFT)
        self.assertEqual(result.target_version.status, ArticleStatus.DRAFT)
        self.assertIsNone(self.article.published_version_id)

    def test_x_lineage_is_recorded_without_changing_old_versions(self):
        old_ids = [version.pk for version in self.versions]
        result = self.controlled(self.actor, self.article, self.versions)
        self.assertEqual(
            list(self.article.versions.values_list("version_number", flat=True)),
            [1, 2, 3, 4],
        )
        audit = AuditLog.objects.get(action=AUDIT_ACTION)
        self.assertEqual(audit.before_data["source_version_id"], old_ids[2])
        self.assertEqual(audit.after_data["target_version_id"], result.target_version.pk)

    def test_dry_run_has_no_mutation(self):
        before = {
            "versions": ArticleVersion.objects.count(),
            "audits": AuditLog.objects.count(),
            "jobs": JobRun.objects.count(),
            "calls": ModelCall.objects.count(),
        }
        result = self.controlled(self.actor, self.article, self.versions, dry_run=True)
        self.article.refresh_from_db()
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.target_version)
        self.assertTrue(result.ready_for_article_review)
        self.assertEqual(self.article.current_version_id, self.versions[2].pk)
        self.assertEqual(before, {
            "versions": ArticleVersion.objects.count(),
            "audits": AuditLog.objects.count(),
            "jobs": JobRun.objects.count(),
            "calls": ModelCall.objects.count(),
        })

    def test_inactive_or_nonpersistent_actor_is_blocked(self):
        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        with self.assertRaises(ArticleBriefDowngradeError):
            self.controlled(self.actor, self.article, self.versions)
        transient = get_user_model()(username="transient")
        with self.assertRaises(ArticleBriefDowngradeError):
            self.controlled(transient, self.article, self.versions)

    def test_no_review_publish_digest_and_one_idempotent_job_run(self):
        review_before = Review.objects.count()
        jobs_before = JobRun.objects.count()
        result = self.controlled(self.actor, self.article, self.versions)
        self.assertEqual(Review.objects.count(), review_before)
        self.assertEqual(JobRun.objects.count(), jobs_before + 1)
        self.assertEqual(result.job_run.status, "SUCCESS")
        self.assertIsNone(result.target_version.article.published_version_id)

    def test_management_command_dry_run_and_apply(self):
        dry_output = StringIO()
        with (
            patch("apps.publishing.brief.CONTROLLED_ARTICLE_ID", self.article.pk),
            patch(
                "apps.publishing.brief.CONTROLLED_SOURCE_VERSION_ID",
                self.versions[2].pk,
            ),
            patch(
                "apps.publishing.brief.CONTROLLED_SOURCE_CONTENT_HASH",
                brief._content_hash(self.versions[2]),
            ),
        ):
            call_command(
                "downgrade_article_to_brief",
                source_version=self.versions[2].pk,
                actor=self.actor.username,
                dry_run=True,
                stdout=dry_output,
            )
            apply_output = StringIO()
            call_command(
                "downgrade_article_to_brief",
                article=self.article.pk,
                actor=self.actor.username,
                stdout=apply_output,
            )
        self.assertTrue(json.loads(dry_output.getvalue())["dry_run"])
        self.assertTrue(json.loads(apply_output.getvalue())["ready_for_article_review"])

    def test_management_command_requires_explicit_selector(self):
        with self.assertRaises(CommandError):
            call_command(
                "downgrade_article_to_brief",
                actor=self.actor.username,
                stdout=StringIO(),
            )


class ArticleBriefDowngradeConcurrencyTests(
    BriefDowngradeFixtureMixin, TransactionTestCase
):
    reset_sequences = True

    def test_o_concurrent_downgrade_creates_only_one_version_four(self):
        actor, article, versions, _, _, _ = self.make_fixture("concurrent-brief")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                results.append(downgrade_article_to_brief(
                    actor=actor, source_version_id=versions[2].pk
                ))
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch("apps.publishing.brief.CONTROLLED_ARTICLE_ID", article.pk),
            patch(
                "apps.publishing.brief.CONTROLLED_SOURCE_VERSION_ID",
                versions[2].pk,
            ),
            patch(
                "apps.publishing.brief.CONTROLLED_SOURCE_CONTENT_HASH",
                brief._content_hash(versions[2]),
            ),
        ):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(article.versions.filter(version_number=4).count(), 1)
        self.assertEqual(article.versions.filter(version_number=5).count(), 0)
        self.assertEqual(AuditLog.objects.filter(action=AUDIT_ACTION).count(), 1)
        self.assertEqual(JobRun.objects.filter(job_type="ARTICLE_BRIEF_DOWNGRADE").count(), 1)
        self.assertEqual(sum(result.already_applied for result in results), 1)
