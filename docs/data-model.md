# Stage 2 Data Model: Core Traceability

This document describes the persisted traceability chain through deterministic feed ingestion and
event clustering. Scoring, AI, image processing, email, review workflow, and publishing workflow are
not implemented.

## Models

- `Source`: source identity, classification, reliability, licensing, crawl policy, and optional
  ownership/independence group metadata.
- `RawItem`: discovery metadata and deduplication fields plus a nullable Event link; it deliberately
  does not store third-party article full text.
- `Event`: a concrete event with first/last observation times, lifecycle status, normalized title,
  deterministic cluster metadata, and calculated source counts.
- `Claim`: an exact-normalized, typed, verifiable statement attached to an event.
- `ClaimEvidence`: a minimal evidence excerpt linking a claim to a RawItem and its source, with an
  explicit support/contradiction/mention/unclear stance.
- `ScoreRuleVersion` and `Score`: immutable-used versioned configuration and deterministic,
  traceable six-dimension Event score results.
- `Article` and `ArticleVersion`: logical article identity separated from immutable content revisions.
- `ArticleVersionClaim`: explicit claims used by a content version.
- `Review`: human review record, separate from article risk level.
- `Image`: image metadata and licensing state only; no search, download or generation.
- `JobRun` and `JobStep`: idempotent job and step execution records.
- `ModelCall`: bounded, costed, version-linked structured LLM call record with job traceability.
- `PromptVersion`: immutable-used system prompt and output schema version for one LLM purpose.
- `ModelPricing`: provider/model-specific versioned token pricing used for pre-call budget checks.
- `ClaimCandidate` and `ScoreSuggestion`: validated LLM proposals isolated from formal Claims,
  Evidence, final Scores, and recommendation levels until explicit human acceptance.
- `ClaimCandidateEvidenceHint`: a proposed RawItem quote with explicit valid/invalid status and a
  bounded failure reason; valid hints can become `MENTIONS` Evidence only after candidate acceptance.
- `AuditLog`: management/content operation record with before/after JSON.

## Relationships and traceability

`Source -> RawItem -> Event -> Claim -> Article` provides the primary information path. An Event has
many RawItems, and deleting an Event sets their nullable Event links to NULL rather than deleting raw
records. `RawItem -> ClaimEvidence -> Claim -> Event -> Article -> ArticleVersion ->
ArticleVersionClaim -> Claim` provides bidirectional evidence navigation. Reviews attach to a specific
article version. Job steps and model calls attach to a JobRun.

`Article.current_version` is nullable and points to an `ArticleVersion`; model validation rejects a version owned by another article. Versions remain historical rows and are never overwritten by this layer.

## Constraints and indexes

- `Source.url` and non-null `identifier` are unique; domain and `source_group` are indexed. Reliability
  is constrained to 0..1.
- Non-empty `RawItem(source, external_id)`, `(source, canonical_url)`, and `(source, content_hash)`
  pairs are unique. Source, Event, status, and published/discovered timestamps have focused indexes.
- Event cluster confidence is constrained to 0..1. Normalized title has a `gin_trgm_ops` index;
  first/last-seen and RawItem Event/publication windows are indexed for candidate filtering.
- `Claim(event, normalized_statement)` and `ClaimEvidence(claim, raw_item,
  normalized_evidence_text)` are unique to prevent deterministic duplicates.
- Model validation rejects empty or overlong evidence and keeps the Claim Event, RawItem Event, and
  Evidence/RawItem Source aligned. Image versions remain aligned with their Article.
- `ArticleVersion(article, version_number)` is unique, preserving revision ordering.
- `ArticleVersionClaim(article_version, claim)` is unique, preventing duplicate claim links.
- `ScoreRuleVersion.version` and `JobRun.idempotency_key` are unique when present; `Score` requires
  exactly one Event or Article target. Event/Rule and Article/Rule pairs are unique, and each score
  dimension plus the calculated final score is constrained to 0..10.
- Lifecycle/status fields and time fields have focused indexes for queue and recency queries; foreign keys use Django's automatic indexes.

## Status enums

Choices are used for RawItem, Event, Claim verification, Score, Article/ArticleVersion, Review, Image, JobRun/JobStep and ModelCall. Risk levels are `LOW`, `MEDIUM`, `HIGH`; review decisions are `PENDING`, `APPROVED`, `REJECTED`, `NEEDS_EDIT`; job states include pending/running/retrying/paused/success/failed/cancelled; image states are pending/ready/failed and license states are `UNKNOWN`, `PENDING`, `PERMITTED`, `RESTRICTED`, `EXPIRED`.

## Storage and boundaries

PostgreSQL 17 with migration-managed `pg_trgm` remains the database. Deterministic Claim evaluation
uses evidence stance, Source tier, and `source_group`; details are in `claim-evidence.md`. No pgvector, Redis, Celery,
Node.js, new database, or second user system is introduced. Review and audit users use
`AUTH_USER_MODEL`. Feed ingestion, Event clustering, Claim validation, deterministic information
scoring, and structured LLM proposal analysis are implemented; LLM boundaries are documented in
`structured-llm-analysis.md`. Article generation, images, email, publishing, public ingress,
personalization, and user recommendation behavior remain excluded.
