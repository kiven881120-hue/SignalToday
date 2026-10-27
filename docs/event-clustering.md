# Deterministic Event Clustering

Stage 2-3 links unassigned `RawItem` records to `Event` records without AI, embeddings, or semantic
models. RawItems remain intact; clustering only changes the nullable Event foreign key.

## Candidate and decision pipeline

Titles are HTML-decoded, Unicode NFKC-normalized, case-folded, whitespace-normalized, and stripped of
common punctuation and an explicitly recognizable media suffix. Digits, dates, countries, companies,
people, product models, and technical terms are retained. A SHA-256 of this normalized title is stored
as a non-unique cluster key.

PostgreSQL `pg_trgm` filters at most 20 Event candidates with similarity >= 0.30 inside the configured
72-hour window. A missing publication time falls back to discovery time; Event occurrence time falls
back to first-seen time. A failed trigram query returns `NO_ACTION` rather than creating a potentially
duplicate Event.

Each candidate is then scored transparently:

- 55% PostgreSQL trigram similarity
- 30% mixed Chinese-bigram/English-token overlap
- 15% time proximity
- a full rejection penalty for a detected conflict

Exact normalized titles receive confidence 1.0. A candidate with trigram >= 0.65 and token overlap >=
0.80 is raised to the 0.90 automatic threshold when conflict-free. Final confidence >= 0.90 assigns
automatically, 0.75 through <0.90 requests manual review, and lower confidence creates a separate
Event. These values describe match confidence, not information importance.

## Conflict protection

Different numbers, years, percentages, basis-point values, product numbers, recognized regions,
organizations, or people prohibit automatic merging. The dictionaries are intentionally small and
transparent. Unknown or ambiguous cases therefore favor a new Event or manual review.

## Consistency, audit, and operations

Assignments run in transactions with a locked RawItem row. A PostgreSQL advisory lock serializes
clustering decisions so concurrent workers cannot assign one item twice or create competing Events
from simultaneous candidates. Event first/last-seen values are recalculated from all linked RawItems,
using publication time with discovery time as fallback.

Every automatic creation, automatic merge, and Admin reassignment appends an `AuditLog` containing old
and new Event IDs, method, confidence, reason, JobRun ID, and timestamp. Admin unlinking is audited too.
Source counts use distinct Source IDs; independent-source counts use `source_group`, treating each
ungrouped Source separately.

Run `python manage.py cluster_events --limit 100 --dry-run` to preview structured decisions without any
database writes. Formal runs process only `event IS NULL` records and create one `EVENT_CLUSTER`
JobRun and cluster JobStep with input/new/assigned/review/skipped/error counts. `--source IDENTIFIER`
restricts the batch. Repeated formal runs are idempotent because assigned rows are excluded.

Known limitation: deterministic dictionaries cannot identify every entity alias or syndication
relationship. Uncertain matches deliberately remain separate or require manual review. No clustering
schedule is enabled automatically.
