# Source Policy

## Access and rights

- An accessible Feed or API does not grant republication rights.
- SignalToday uses RSS/Atom only to discover events and retain necessary metadata: source, title,
  link, publication time, and a short feed-provided summary when present.
- The ingestion system does not visit article pages or store complete third-party articles.
- Feed summaries are treated as untrusted, converted to plain text, and limited to 2,000 characters.
- Future published explanations must be original system output and must retain source links and
  attribution. Their implementation is outside this stage.
- Paid access does not grant commercial scraping or republication permission.

## Source tiers

- **A:** primary official authorities, institutions, and original issuers; suitable for primary evidence.
- **B:** highly reliable professional reporting or specialist institutions; suitable for corroboration.
- **C:** useful secondary sources with variable primary access; verify important facts.
- **D:** community sources; discovery leads only and must be verified by reliable sources.
- **E:** unknown, low-confidence, or restricted sources; disabled unless explicitly reviewed.

Search is a discovery mechanism, not primary factual evidence. Sources must be active, explicitly
configured, pass URL/DNS security validation, and have a feed-only crawl policy before ingestion.
