from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils.text import slugify


class SiteTheme(models.TextChoices):
    DEFAULT = "DEFAULT", "Default"
    DARK = "DARK", "Dark"
    WARM = "WARM", "Warm"


class SiteSettings(models.Model):
    site_name = models.CharField(max_length=100, default="SignalToday")
    logo_url = models.URLField(blank=True, default="")
    theme = models.CharField(max_length=20, choices=SiteTheme.choices, default=SiteTheme.DEFAULT)
    primary_color = models.CharField(
        max_length=7, default="#0f172a",
        validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$", "Enter a #RRGGBB hex color.")],
    )
    digest_send_hour = models.PositiveSmallIntegerField(
        default=8, validators=[MinValueValidator(0), MaxValueValidator(23)],
    )
    automatic_publish_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    def __str__(self):
        return self.site_name

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Category(models.Model):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True, blank=True)
    weight = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("1.00"),
        validators=[MinValueValidator(0), MaxValueValidator(10)],
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-weight", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True)
        super().save(*args, **kwargs)


class LLMProviderPreset(models.Model):
    name = models.CharField(max_length=100, unique=True)
    provider = models.CharField(max_length=30, default="openai-compatible")
    base_url = models.URLField()
    default_model = models.CharField(max_length=100)
    notes = models.TextField(blank=True)
    is_builtin = models.BooleanField(default=False)

    def __str__(self):
        return self.name
