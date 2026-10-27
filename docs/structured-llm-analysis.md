# Structured LLM Analysis Foundation

Stage 2-6 treats an LLM as an untrusted analyst. It can propose ClaimCandidate and ScoreSuggestion
records, but it cannot create ClaimEvidence, publish content, set a final Score, choose a
recommendation level, translate content, or send messages.

## Configuration

Runtime configuration is read only from `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_BASE_URL`, and
`LLM_MODEL`. Optional limits configure the CNY 20 daily budget, timeout, and output tokens. The API
key is never persisted or logged. The initial provider is OpenAI-compatible behind the generic
`LLMProvider.analyze_structured()` interface; business services do not import a vendor SDK.

ModelPricing stores provider-specific input/output prices per million tokens, currency, effective
date, and version. No provider price is assumed. A formal call pauses when no active CNY price or API
key is configured. Calls at projected 100% daily budget pause; at 80%, warnings are emitted and
low-level Event analysis pauses. The configured model is never silently changed.

## Input and output controls

At most 10 RawItems are selected, ordered by Source tier A through E and deduplicated by Source.
Only Event metadata, title, stored summary metadata, Source/tier, and publication time are used.
Each item is capped at 3,000 characters and the user prompt at 30,000 characters. Source material is
HTML-escaped, placed inside an explicit untrusted boundary, and cannot alter the system prompt.

Claim extraction and score suggestion use strict, versioned JSON schemas with no additional fields.
Parsing enforces JSON-only output, required fields, enums, numeric ranges, string lengths, item count,
and a 20,000-character raw-output limit. One separately billed and recorded schema repair call is
allowed. A second schema failure stops processing.

## Traceability and review

Every attempted provider request records ModelCall timing, tokens, estimated/actual cost, pricing,
status, bounded error/output, PromptVersion, schema version, JobRun, and JobStep. Retryable timeout,
429, and 5xx-style errors use at most three attempts with exponential backoff and jitter. Permanent
configuration/auth/request errors do not retry.

An operator may retry one failed Claim Extraction execution with
`python manage.py retry_claim_analysis --job-run <id> --reason <text>`. The source JobRun must be
`FAILED`, and its latest Claim Extraction ModelCall must be `TIMEOUT` or `FAILED`. The retry keeps
the source records unchanged, creates a new audited JobRun/JobStep/ModelCall, and uses the source
JobRun ID as an idempotency identity so repeating the same request does not call the provider twice.
Provider retry limits, budget enforcement, schema validation, and Candidate/EvidenceHint uniqueness
remain in effect.

The analysis key is Event + purpose + PromptVersion + model. PostgreSQL advisory locks serialize the
same analysis task and successful results are reused. ClaimCandidate acceptance creates a normal
Claim through existing validation and converts only validated hints to MENTIONS Evidence. ScoreSuggestion acceptance invokes the
existing deterministic scoring engine and ignores any model-supplied final score. Accept/reject
transitions are recorded in AuditLog without prompts or responses. Candidate `importance_hint` is
not copied into formal Claim importance. Accepting a suggestion links an existing current Score
without recalculating it, preserving prior human adjustments; deterministic scoring runs only when
no current Score exists.

## Commands

```bash
python manage.py analyze_events --purpose claims --dry-run
python manage.py analyze_events --purpose scoring --event 1 --limit 10
```

Dry-run never calls a provider or writes ModelCall, candidate, suggestion, JobRun, or AuditLog.
Formal execution creates an `LLM_EVENT_ANALYSIS` JobRun and JobStep. Configure a real pricing row and
runtime credentials before formal provider use.

## Claim extraction v1.2

The active `CLAIM_EXTRACTION` prompt is `v1.2`. It treats Event and RawItem fields as untrusted data,
uses at most ten tier-ordered sources, removes duplicate sources and source groups, and sends only
bounded titles and existing summaries. It never fetches article bodies.

The `claim-extraction-v2` response contains zero or more Claims, an integer-or-null 0-100
`importance_hint`, a bounded conditional `importance_reason`, and structured evidence hints. The
hint is review context only and never becomes formal Claim importance. Each
hint names a RawItem and quotes at most 2,000 characters. Code accepts a hint only when the RawItem
belongs to the Event and its NFKC/whitespace-normalized quote occurs in the exact material sent to
the model. Numbers, percentages, dates, amounts, and alphanumeric product models in the Claim must
also occur in the quote. Invalid hints remain traceable and cap unsupported candidate confidence at
0.5; they do not fail the whole Event.

Exact normalized Claims are deduplicated per Event and their distinct hints are merged. Accepting one
PROPOSED candidate creates or reuses one UNVERIFIED Claim, converts only valid hints to MENTIONS
ClaimEvidence, and runs the deterministic Claim evaluator. Acceptance is idempotent. It never marks
LLM evidence as SUPPORTS, activates importance, or accepts candidates in bulk. Historical v1.1 and
`claim-extraction-v1` records remain supported for replay.
