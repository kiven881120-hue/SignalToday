# SignalToday

SignalToday is a mobile-first information radar built around one principle: spend less time scrolling and understand more.

This repository currently contains the Stage 1 infrastructure only. Collection, AI generation, clustering, scoring, images, email delivery, user accounts, and public deployment are intentionally not implemented.

## Local Development

Prerequisites: Docker Engine and Docker Compose.

```bash
cp .env.example .env
# Replace every required placeholder in .env before starting.
docker compose build
docker compose run --rm web python manage.py migrate
docker compose up -d
docker compose ps
```

The local health endpoint is `http://127.0.0.1:8000/health/` and Django Admin is available at `http://127.0.0.1:8000/admin/`.

Run the test suite with:

```bash
docker compose run --rm web python manage.py test
```

## Architecture

- `web`: Django 5.2 LTS served by Gunicorn.
- `scheduler`: a separate APScheduler process using the same application image.
- `postgres`: PostgreSQL 17 with `pg_trgm` enabled.

PostgreSQL uses a Docker named volume under Docker's local data root. Do not point it at a network/CIFS/NFS mount as the online data directory.

## License

MIT. See [LICENSE](LICENSE).

## Content And Source Disclaimer

SignalToday ingests headlines and metadata from third-party sources you configure (see `config/sources/`). It is designed to summarize and link back to original reporting, not to republish full third-party articles. Copyright in the underlying source content belongs to the original publishers.

If you self-host this project, you are responsible for complying with the terms of service, copyright law, and any applicable regulations for the sources you choose to ingest and the jurisdictions you operate in. This project provides no warranty and assumes no liability for how it is deployed or operated by others; see the LICENSE for the full disclaimer.
