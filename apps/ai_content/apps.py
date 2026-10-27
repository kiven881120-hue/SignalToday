from django.apps import AppConfig


class AiContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ai_content"

    def ready(self):
        from . import signals  # noqa: F401
