# Feed Ingestion

## Architecture

The pipeline is `Source config -> Source -> safe HTTP -> collection parser -> normalized entry ->
optional article content -> deterministic dedupe -> RawItem`. `JobRun` and seven `JobStep` rows
record source validation, fetch, parse, normalization, content, dedupe, and persistence. Job
summaries contain counts and hosts, never response bodies or credentials.

Source definitions are JSON files in `config/sources/`. `sync_sources` validates the complete schema,
types, enum values, feed URL/domain agreement, reliability range, and `feed_only` policy before one
atomic database sync. The stable unique `identifier` makes repeated syncs idempotent.

Sitemap Sources may define collection-only `path_allow_prefixes`, `path_allow_patterns`,
`path_deny_prefixes`, `source_freshness_days`, and `source_scope_limit`. Detail paths are
allowlisted before persistence;
section roots, out-of-scope paths, entries without `lastmod`, stale entries, and entries beyond the
bounded limit are excluded. This source scope controls what can become a future `RawItem`; it is
separate from downstream business selection windows such as the 14-day candidate rule. Sitemap
`lastmod` remains `content_updated_at` and is never treated as `published_at`.

## Safe HTTP

`SafeHttpClient` is the only remote fetch layer. Defaults are a 5-second connect timeout, 15-second
read timeout, 5 redirects, 5 MiB streamed response limit, and a named User-Agent. Only HTTP(S) and
XML/RSS/Atom content types are accepted. Redirects are followed manually and every target is fully
revalidated. Timeout, 429, and 500/502/503/504 failures receive at most three retries with exponential
backoff and jitter; permanent HTTP failures and security rejection are not retried.

The two pinned ingestion dependencies are `urllib3` for streamed, manually redirected HTTP transport
and `defusedxml` for hardened XML parsing. No queue, browser, or new infrastructure is introduced.

Before each request, hostname DNS records are resolved and every returned address must be globally
routable. Loopback, private, link-local, multicast, unspecified, reserved, known metadata IPs and
metadata hostnames are rejected for IPv4 and IPv6. The client connects directly to one previously
validated IP while retaining the original Host header and HTTPS SNI/certificate hostname, preventing
a second DNS lookup during connection. This conservative pinning and redirect revalidation reduce
DNS-rebinding/TOCTOU exposure. Residual risk remains in upstream routing and DNS data changing between
separate runs; no application-only implementation can control those layers.

## Parsing and normalization

The parser supports RSS 2.0 and Atom with `defusedxml`, which rejects DTD entities and external entity
resolution. Feed HTML summaries are reduced to plain text, dangerous container contents are removed,
and output is capped at 2,000 characters. A Source may explicitly opt into bounded article-page
fetching with `fetch_article_body` and `article_body_limit`. Article URLs and final redirects must
remain on the configured Source domain and use the same safe HTTP path. The generic extractor favors
article/main/content/editor containers, removes script/style/nav/form and clear metadata regions,
and stores the complete cleaned body in `RawItem.raw_metadata.body`. Failed or empty extraction is
marked `INSUFFICIENT`; title, URL, source, author and publication metadata are never body fallbacks.

Canonical URLs lowercase scheme/host, encode IDNs as punycode, remove fragments and default ports,
normalize an empty path to `/`, and remove only the five declared `utm_*` parameters. Unknown query
parameters are preserved.

The SHA-256 content hash is computed from whitespace-normalized title, canonical URL, and UTC ISO-8601
publication time joined by newline characters. Fetch/discovery time is excluded. Dedupe checks, in
order, source/external ID, source/canonical URL, and source/content hash. Conditional database unique
constraints provide final concurrent protection.

## Transactions, jobs, and operations

Persistence uses one transaction per Source, with nested savepoints for expected uniqueness races;
one source failure cannot roll back another source. Existing RawItems may receive a newly extracted
article body without changing their identity; repeating the same content is a no-op. Run
`sync_sources`, then `ingest_sources` or
`ingest_sources --source IDENTIFIER --dry-run`. An optional idempotency key is supported for a single
source. Logs contain source ID, host, status and counts only.

The scheduler exposes a callable hook but does not register automatic real ingestion. It runs only if
an operator explicitly sets `INGESTION_SCHEDULE_ENABLED=true` and wires the hook into a schedule.

Known limitations: V1 handles RSS 2.0 and Atom rather than every historical feed extension, uses one
validated address per request without failover, and performs deterministic rather than semantic
deduplication. Event clustering, scoring, AI, publishing, images, and email are later-stage work.
