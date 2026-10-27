# Deterministic Information Importance Scoring

Stage 2-5 scores Events with a versioned, auditable rule. No LLM, external API, personalization,
publishing, Source mutation, RawItem mutation, or ClaimEvidence mutation is involved.

## Default rule

Migration `scoring.0003_information_importance_scoring` creates active rule `v1.0`, named
`default-information-value-v1`. Its weights are stored in `ScoreRuleVersion.rule_config`:

- importance: 0.25
- credibility: 0.25
- user_relevance: 0.15
- information_gain: 0.15
- freshness: 0.10
- long_term_impact: 0.10

Weights must be non-negative and total exactly 1. Thresholds must descend and stay in 0..10:
FOCUS=8.0, NORMAL=6.5, CANDIDATE=5.0. Lower scores are REJECT. A rule referenced by a Score is
immutable; create a new version to preserve scoring history.

## Dimensions

All dimensions and the final score are Decimal values in 0..10.

- Importance uses the highest human-maintained Claim importance and never source volume.
- Credibility reads Claim verification status and explicit Evidence. Conflicted, contradicted, and
  unverified Claims cap it below the high range. Official evidence and independent reliable
  `source_group` values can raise fully supported Events to the high range.
- User relevance uses explicit IMPACT Claim importance, with a conservative fallback capped at 5.
- Information gain uses distinct supported or partially supported Claim count, never RawItem or source count.
- Freshness uses occurred time, then first-seen time, with timezone-aware fixed buckets.
- Long-term impact uses explicit IMPACT Claims, with a conservative fallback capped at 5.

The final score is calculated in code with weights read from the selected database rule. Score.save
recalculates both final score and recommendation level, so callers and Admin cannot override them.
Structured reasons include one reason per dimension plus `recommendation_reason`.

## Operation

```bash
python manage.py score_events --dry-run
python manage.py score_events --limit 100 --event 1 --status OPEN --rule-version v1.0
```

Dry-run creates no Score, JobRun, or AuditLog. A formal run locks each Event, updates or creates the
single `(event, rule_version)` Score atomically, and records an `EVENT_SCORING` JobRun/JobStep.
Score changes are audited with old/new score and level, rule version, reason, job ID, and timestamp.
