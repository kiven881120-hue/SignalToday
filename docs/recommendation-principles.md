# Recommendation principles

SignalToday does not decide what a user is allowed to value. It helps users see
what they already care about and discover reliable information they may not
normally encounter.

The unchanged Score dimensions have distinct meanings: objective importance,
user relevance, information gain, freshness, credibility, and long-term impact.
The aggregate recommendation level is a ranking tier, not a factual-worth or
interest verdict. Low user relevance alone must never exclude an otherwise
verified item from article generation or publication review.

The deterministic recommendation interface exposes four lanes:

- `MUST_KNOW`: objectively important information that is easy to miss.
- `PERSONALIZED`: reserved for explicit user interest signals. No inferred user
  profile is implemented in this phase.
- `DISCOVER`: high-information-gain material with lower current user relevance,
  or material with an explicit future potential-interest signal.
- `TOPIC`: reliable published information organized by subject. Unknown topics
  fall back to `GENERAL`, not `DISCOVER`.

Human review, evidence traceability, Grounding, and Quality remain publication
gates. Recommendation lanes never relax factual standards.
