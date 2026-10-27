# Score refresh

An Event Score under the current active rule is a mutable current projection.
When its Claim or Evidence inputs are known to have changed, invalidate it first:

```sh
python manage.py invalidate_event_score --event 1 --reason "claim/evidence verification changed"
```

Invalidation changes only `Score.status` from `CALCULATED` to `INVALID`. It is
idempotent, uses an Event row lock, and records the complete before/after Score
snapshot in AuditLog. Article eligibility ignores the invalid Score.

After deterministic Claim evaluation is complete, refresh that one Event:

```sh
python manage.py refresh_event_score --event 1
```

Refresh reuses the existing deterministic scoring calculation, locks the Event
and Score in one transaction, updates the existing Event+Rule Score, and records
a JobRun, JobStep, and complete AuditLog snapshot. A failed refresh leaves an
already invalidated Score `INVALID`, so eligibility remains closed and the same
command can be retried safely.

These commands do not call an LLM or create a replacement Score. They do not
automatically connect Evidence Review to Claim Evaluation; orchestration,
freshness scheduling, rule-activation fan-out, and batch refresh remain outside
this initial mechanism.
