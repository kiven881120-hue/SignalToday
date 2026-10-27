from django.contrib import admin

from .models import Category, LLMProviderPreset, SiteSettings


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ("site_name", "theme", "automatic_publish_enabled", "updated_at")
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        from django.urls import reverse

        obj = SiteSettings.load()
        return redirect(reverse("admin:settings_sitesettings_change", args=[obj.pk]))

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "weight", "active")
    list_editable = ("weight", "active")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug")


@admin.register(LLMProviderPreset)
class LLMProviderPresetAdmin(admin.ModelAdmin):
    list_display = ("name", "provider", "base_url", "default_model", "is_builtin")
    list_filter = ("provider", "is_builtin")
    search_fields = ("name", "provider", "base_url", "default_model")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_builtin:
            return False
        return super().has_delete_permission(request, obj)
