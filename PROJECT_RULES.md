# SignalToday Project Rules

1. Do not damage the host system or existing services.
2. Do not modify services unrelated to SignalToday.
3. Never commit secrets, credentials, tokens, cookies, or production environment files.
4. Preserve the source of all third-party content.
5. Do not copy full third-party media articles.
6. Do not implement keyword transformations or other mechanisms intended to evade regulation.
7. High-risk content requires human review.
8. Every final factual claim must be traceable to evidence.
9. Production jobs must be idempotent and recoverable.
10. Run relevant tests after every code change.
11. Do not add complexity without a demonstrated need.
12. V1 prioritizes stability and content quality.
13. PostgreSQL online data must remain on local, low-latency storage (not a network/CIFS/NFS mount).
14. Network storage (NAS, object storage, etc.) is a backup target only, never the live database directory.
15. Do not introduce Redis, Celery, pgvector, Node, or other infrastructure without approval.
