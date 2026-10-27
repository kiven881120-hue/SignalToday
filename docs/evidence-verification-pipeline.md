# Evidence verification pipeline

The single-Evidence pipeline composes existing deterministic services without
duplicating their business rules:

```text
Evidence Review -> Claim Evaluation -> Score Invalidation
                -> Score Refresh -> Article Eligibility Check
```

Run it for one Evidence record:

```sh
python manage.py verify_evidence_pipeline --evidence 1
```

One `EVIDENCE_VERIFICATION_PIPELINE` JobRun is retained under the idempotency key
`evidence-verification:<id>`. A successful run is returned unchanged. A failed
run requires an explicit retry and resumes from its first non-successful step:

```sh
python manage.py verify_evidence_pipeline --evidence 1 --retry
```

Each stage has its own transaction. Evidence Review and Claim Evaluation commit
before Score invalidation; a refresh failure therefore leaves the verified
Claim intact and the Score `INVALID`, which keeps article eligibility closed.
The retry does not repeat successful stages.

If Evidence is already `SUPPORTS`, its Claim is already `SUPPORTED`, and its
current Score is `CALCULATED`, the mutation stages are recorded as skipped and
only the read-only eligibility decision is evaluated. The pipeline never calls
an LLM, creates an Article, or publishes content.
