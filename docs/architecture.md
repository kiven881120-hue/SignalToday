# Stage 1 Architecture

SignalToday is a modular Django monolith with three Stage 1 services:

- `web`: Gunicorn serving Django. It never starts APScheduler.
- `scheduler`: a single dedicated process running `manage.py run_scheduler`.
- `postgres`: PostgreSQL 17 with `pg_trgm`; no pgvector.

Both application services use the same immutable image. PostgreSQL uses the named volume `signaltoday_postgres_data`, stored under Docker Root Dir on the host's local ext4 SSD.

The scheduler heartbeat proves process separation, database connectivity, and PostgreSQL advisory lock availability. Persistent JobRun and JobStep models are reserved for Stage 2.
