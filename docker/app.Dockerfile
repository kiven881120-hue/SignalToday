FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY . /app
RUN python -m pip install --no-cache-dir . \
    && DJANGO_SECRET_KEY=build-only DATABASE_URL=postgresql://build:build@localhost/build DJANGO_SETTINGS_MODULE=project.settings.prod python manage.py collectstatic --noinput \
    && chown -R app:app /app

USER app

EXPOSE 8000
