import decimal

import django.db.models.deletion
import django.core.validators
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("site_name", models.CharField(default="SignalToday", max_length=100)),
                ("logo_url", models.URLField(blank=True, default="")),
                (
                    "theme",
                    models.CharField(
                        choices=[("DEFAULT", "Default"), ("DARK", "Dark"), ("WARM", "Warm")],
                        default="DEFAULT", max_length=20,
                    ),
                ),
                (
                    "primary_color",
                    models.CharField(
                        default="#0f172a", max_length=7,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^#[0-9A-Fa-f]{6}$", "Enter a #RRGGBB hex color."
                            )
                        ],
                    ),
                ),
                (
                    "digest_send_hour",
                    models.PositiveSmallIntegerField(
                        default=8,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(23),
                        ],
                    ),
                ),
                ("automatic_publish_enabled", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+", to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Category",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50, unique=True)),
                ("slug", models.SlugField(blank=True, max_length=50, unique=True)),
                (
                    "weight",
                    models.DecimalField(
                        decimal_places=2, default=decimal.Decimal("1.00"), max_digits=4,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(10),
                        ],
                    ),
                ),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-weight", "name"]},
        ),
        migrations.CreateModel(
            name="LLMProviderPreset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("provider", models.CharField(default="openai-compatible", max_length=30)),
                ("base_url", models.URLField()),
                ("default_model", models.CharField(max_length=100)),
                ("notes", models.TextField(blank=True)),
                ("is_builtin", models.BooleanField(default=False)),
            ],
        ),
    ]
