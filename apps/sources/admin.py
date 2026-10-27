from django.contrib import admin

from .models import Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name", "domain", "source_group", "source_type", "tier", "reliability", "active", "license_state",
    )
    list_filter = ("source_type", "tier", "active", "license_state")
    search_fields = ("identifier", "name", "domain", "organization", "source_group")
    readonly_fields = ("created_at", "updated_at")
