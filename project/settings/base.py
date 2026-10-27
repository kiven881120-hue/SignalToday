import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


BASE_DIR = Path(__file__).resolve().parents[2]


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = env_bool("DJANGO_DEBUG")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.postgres",
    "django.contrib.staticfiles",
    "apps.core.apps.CoreConfig",
    "apps.settings.apps.SettingsConfig",
    "apps.sources.apps.SourcesConfig",
    "apps.ingestion.apps.IngestionConfig",
    "apps.events.apps.EventsConfig",
    "apps.claims.apps.ClaimsConfig",
    "apps.scoring.apps.ScoringConfig",
    "apps.ai_content.apps.AiContentConfig",
    "apps.reviews.apps.ReviewsConfig",
    "apps.publishing.apps.PublishingConfig",
    "apps.mailing.apps.MailingConfig",
    "apps.jobs.apps.JobsConfig",
    "apps.audit.apps.AuditConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "project.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
WSGI_APPLICATION = "project.wsgi.application"
ASGI_APPLICATION = "project.asgi.application"

database_url = urlparse(os.environ["DATABASE_URL"])
if database_url.scheme not in {"postgres", "postgresql"}:
    raise ValueError("DATABASE_URL must use PostgreSQL")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": database_url.path.lstrip("/"),
        "USER": unquote(database_url.username or ""),
        "PASSWORD": unquote(database_url.password or ""),
        "HOST": database_url.hostname,
        "PORT": database_url.port or 5432,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EVENT_CLUSTER_TIME_WINDOW_HOURS = 72
EVENT_CLUSTER_CANDIDATE_THRESHOLD = 0.30
EVENT_CLUSTER_AUTO_THRESHOLD = 0.90
EVENT_CLUSTER_REVIEW_THRESHOLD = 0.75

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai-compatible")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_DAILY_BUDGET_CNY = os.getenv("LLM_DAILY_BUDGET_CNY", "20")
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "2000"))
LLM_MAX_ATTEMPTS = 3
LLM_RETRY_JITTER_SECONDS = 0.25
MAX_ANALYSIS_RAW_ITEMS = 10
MAX_ANALYSIS_ITEM_CHARS = 3000
MAX_ANALYSIS_INPUT_CHARS = 30000
MAX_CLAIM_BODY_INPUT_CHARS = 1200

SCHEDULER_PIPELINE_ENABLED = env_bool("SCHEDULER_PIPELINE_ENABLED", False)
SCHEDULER_STALE_JOB_MINUTES = int(os.getenv("SCHEDULER_STALE_JOB_MINUTES", "30"))

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE")
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE")
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact_secrets": {"()": "project.logging_filters.SensitiveDataFilter"}},
    "formatters": {
        "standard": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["redact_secrets"],
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "signaltoday.scheduler": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "signaltoday.ingestion": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "signaltoday.clustering": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "signaltoday.llm": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
