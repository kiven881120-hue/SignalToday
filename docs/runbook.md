# Stage 1 Runbook

## Start

```bash
docker compose up -d
docker compose ps
```

## Validate

```bash
docker compose config --quiet
docker compose exec -T web python manage.py check
docker compose exec -T web python manage.py test
docker compose exec -T scheduler python manage.py run_scheduler --once
curl --fail http://127.0.0.1:8000/health/
```

## Stop

```bash
docker compose down
```

Do not add `--volumes`: the PostgreSQL named volume contains persistent data.

## Inspect

```bash
docker compose ps
docker compose logs --tail=200 web scheduler postgres
docker volume inspect signaltoday_postgres_data
```
