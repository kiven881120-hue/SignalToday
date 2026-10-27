# Article generation eligibility

Article generation must call
`evaluate_article_generation_eligibility(event_or_id)` before any generation or
publishing work. The check is deterministic and read-only: it does not call an
LLM, create an Article or ArticleVersion, or change review state.

An Event is eligible when its current Score under an active rule is calculated
and at least one formal `SUPPORTED` Claim has a
`SUPPORTS` ClaimEvidence record whose quote is traceable to a RawItem in the
same Event and whose Source matches that RawItem. `ClaimCandidate` records are
never generation facts. `FOCUS`, `NORMAL`, `CANDIDATE`, and `REJECT` remain
unchanged ranking tiers; they control recommendation priority and presentation,
not whether a verified niche fact has value.

Unsupported, partially supported, rejected, and unverified Claims are excluded.
The presence of a `CONTRADICTED` or `CONFLICTED` Claim blocks the entire Event
until the conflict is resolved; the result still lists any independently safe
Claim and evidence IDs so downstream diagnostics remain explicit.

Callers must require `result.eligible` to be true and use only
`result.eligible_claim_ids` and `result.eligible_evidence_ids`. A reason code is
returned for every rejection so orchestration can stop without guessing or
falling back to raw material.

## Draft generation

Generate or inspect one Event with:

```bash
python manage.py generate_article --event <id>
python manage.py generate_article --event <id> --dry-run
python manage.py generate_article --event <id> --retry
```

The command checks eligibility before preparing a prompt. An ineligible Event
ends successfully with structured reason data and cancelled downstream steps;
it creates no `ModelCall`, Article, version, or Claim link. Dry-run prepares the
grounded input and estimates cost, but likewise performs no LLM call or Article
write.

Real generation uses the active `ARTICLE_GENERATION` prompt and the
`article-generation-v1.3` structured schema. Source material is marked untrusted,
and output Claim IDs are validated again against current eligibility and
traceable SUPPORTS Evidence before a transaction creates a DRAFT Article,
version, and Claim links. The Article-to-Event unique constraint and PostgreSQL
advisory lock prevent concurrent duplicates.

A successful repeat returns the existing draft. A running generation is
rejected. A failed generation requires `--retry`; the retry receives a new
JobRun and ModelCall while the failed records remain available for audit.

An operator can explicitly create a new version for an Event that already has
an Article with:

```bash
python manage.py regenerate_article --event <id> --actor <username> \
  --reason "..." --request-id <stable-request-id>
```

Regeneration uses the active Article Generation prompt and schema. A new
request ID creates an independent JobRun, ModelCall, and ArticleVersion; the
same request ID is write-idempotent. The source version and its review history
remain unchanged, and the source-to-target relationship is audited. A passing
Grounding and Quality gate creates a PENDING Review; otherwise the target stays
DRAFT. When the source is the published version, it remains the formal current
and published version while the regenerated target is staged for review.

## Human review and publication

Article generation stops at `DRAFT`. Human operators use the review service
through these commands (all require an active username and a reason):

```bash
python manage.py submit_article_review --article-version <id> --actor <user> --reason "..."
python manage.py approve_article --article-version <id> --actor <user> --reason "..."
python manage.py reject_article --article-version <id> --actor <user> --reason "..."
python manage.py publish_article --article-version <id> --actor <user> --reason "..."
python manage.py correct_article --article-version <id> --actor <user> --reason "..."
python manage.py retract_article --article-version <id> --actor <user> --reason "..."
```

Only a current version with a pending approved `Review` can publish. Publish
revalidates Eligibility and the complete Claim -> SUPPORTS Evidence -> RawItem
-> Source chain under row locks. Published content and claim links are
immutable; correction creates a new DRAFT version and preserves history.

## Daily Digest

Build the deterministic daily selection with:

```bash
python manage.py generate_daily_digest --date YYYY-MM-DD --dry-run
python manage.py generate_daily_digest --date YYYY-MM-DD
python manage.py generate_daily_digest --date YYYY-MM-DD --rebuild
```

Only current, valid `PUBLISHED` articles whose Event has a current calculated
Score are considered. Candidates are ordered by
recommendation tier, score, publication time, and Article ID; at most 12 are
selected. Categories, the maximum three `MUST_KNOW` positions, and short
recommendation reasons are computed from fixed rules without an LLM.

The structured Article fields remain internal validation controls. Public pages
render the non-empty body fields as consecutive natural paragraphs without the
labels "发生了什么", "为什么重要", "对谁有影响", or "举个例子". In v1.3,
unsupported Why, Who, and Example sections are null and are omitted rather than
filled with absence boilerplate; `what_happened` is also omitted when the
evidence provides no information beyond the title and summary.

One Digest exists per date and one Article per Digest. A READY Digest is
immutable on normal reruns. `--rebuild` is explicit and audited. Dry-run performs
the same selection without creating a Digest, JobRun, item, or AuditLog.
# Content validation gate

Every newly generated `ArticleVersion` receives one `ArticleContentValidation`
record before the generation job completes. The record contains the content
hash, Grounding result, deterministic Quality input, Quality result, validator
versions, and validation timestamps. A non-PASS result leaves the version as a
blocked draft.

Submitting for review, approving, and publishing all require the current
content hash to retain PASS Grounding and Quality results. Publish recomputes
both validators against current Claim/Evidence data, so an old approval or a
stale validation record cannot bypass the gate.

Quality input uses deterministic proposition fingerprints across title,
summary, and body sections. The current Quality validator remains responsible
for information gain, repetition, and thin-content decisions; the input
producer does not use topic keyword rules. Sections explicitly declared
`NONE_ESTABLISHED` are represented as epistemic boundaries, not as established
meaning or affected-party facts.
