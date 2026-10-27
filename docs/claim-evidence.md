# Claim Evidence Validation

Stage 2-4 establishes a deterministic, human-curated evidence chain:

`Event -> RawItem -> ClaimEvidence -> Claim`

It does not extract claims automatically, call AI services, copy full articles, or decide what a news
story should say. Evidence text must be the smallest excerpt needed to support, contradict, mention,
or clarify one Claim and is limited to 2,000 characters.

## Data rules

- A Claim belongs to exactly one Event. Its text is normalized with Unicode NFKC, HTML entity
  decoding, whitespace folding, trimming, and case folding. `(event, normalized_statement)` is unique.
- Claim types are `FACT`, `DATE`, `NUMBER`, `QUOTE`, `LOCATION`, `PERSON`, `ORGANIZATION`,
  `PRODUCT`, and `IMPACT`.
- ClaimEvidence belongs to a Claim and a RawItem. The RawItem must already belong to the same Event,
  and the Evidence source must match the RawItem source.
- Evidence stance is `SUPPORTS`, `CONTRADICTS`, `MENTIONS`, or `UNCLEAR`.

## Evidence review

Human review uses `review_claim_evidence`, never a direct model update. The initial workflow only
allows `MENTIONS -> SUPPORTS`; repeated `SUPPORTS -> SUPPORTS` requests are idempotent, and all other
transitions are rejected. The service locks the Evidence row, revalidates its Claim, RawItem, Source,
Event, and quote, and records the old/new stance and caller-supplied reason in AuditLog atomically.

The internal operations entry point is:

`python manage.py review_claim_evidence --evidence <id> --stance SUPPORTS --reason <text>`

ClaimEvidence stance is read-only in Django Admin so general editing cannot bypass this workflow.
- Empty Evidence types map from Source tier without replacing a manual value: A=`OFFICIAL`,
  B=`NEWS_REPORT`, C=`INDUSTRY_MEDIA`, D=`SOCIAL_LEAD`, E=`SECONDARY`.
- Evidence normalization preserves numbers, dates, and negation. `(claim, raw_item,
  normalized_evidence_text)` is unique.

## Deterministic evaluation

`evaluate_claim(claim)` aggregates Evidence in PostgreSQL. Sources sharing a non-empty
`source_group` count once; an ungrouped Source counts by its own ID.

Evaluation order is conservative:

1. Preserve manually `REJECTED` Claims.
2. Any simultaneous explicit support and contradiction is `CONFLICTED`.
3. Contradiction without support is `CONTRADICTED`.
4. Explicit support from an official/tier-A source is `SUPPORTED`.
5. Explicit support from two independent tier A/B/C sources is `SUPPORTED`.
6. One tier A/B/C non-official support is `PARTIALLY_SUPPORTED`.
7. Mentions, unclear Evidence, social leads alone, and no Evidence remain `UNVERIFIED`.

The rules report their reason; they do not infer truth from matching numbers or source rank.

## Importance review

Formal Claim importance is either unassessed (`NULL`) or an integer from 0 through 100. A positive
value requires a `SUPPORTED` Claim and at least one `SUPPORTS` Evidence row. Candidate hints remain
suggestions and are never copied into this field. A later loss of `SUPPORTED` status clears a
positive importance and invalidates the current Event Score.

Human operators use the transactional `review_claim_importance` service through the narrow command:

```bash
python manage.py review_claim_importance \
  --claim <id> --importance <0-100|null> \
  --expected-importance <0-100|null> --expected-status SUPPORTED \
  --expected-claim-type <current-type> --claim-type <new-type> \
  --actor <username> --reason <text>
```

`--claim-type` is optional when the type is unchanged. The service locks Event, Claim, and current
Score in order, rejects stale or unsupported updates, records the human actor and evidence IDs,
invalidates the Score atomically, then reuses Claim Evaluation and Score Refresh. A downstream
failure leaves the Score `INVALID`; retrying the identical request resumes the incomplete workflow
without a duplicate state-change audit.

## Operation

```bash
python manage.py evaluate_claims --dry-run
python manage.py evaluate_claims --limit 100 --event 1 --status UNVERIFIED
```

Dry runs create no JobRun, AuditLog, or status update. Formal runs create a `CLAIM_EVALUATION`
JobRun and JobStep, lock each Claim during evaluation, update changed statuses atomically, and audit
the old/new status, reason, job ID, and timestamp. Evidence create/update/delete actions are audited
with identifiers and a content hash; copied excerpt text is deliberately excluded from AuditLog.

Admin exposes Claims inline on Event, Evidence inline on Claim, and direct Claim/Evidence views.
