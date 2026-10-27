from django.contrib import admin

from .models import Review


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = (
        "article_version", "grounding_status", "quality_status",
        "reviewer", "risk_level", "decision", "reviewed_at",
    )
    list_filter = ("risk_level", "decision")
    search_fields = ("comment",)
    readonly_fields = (
        "article_version", "reviewer", "risk_level", "decision", "comment",
        "reviewed_at", "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Grounding")
    def grounding_status(self, obj):
        gate = getattr(obj.article_version, "content_validation", None)
        return gate.grounding_status if gate else "MISSING"

    @admin.display(description="Quality")
    def quality_status(self, obj):
        gate = getattr(obj.article_version, "content_validation", None)
        return gate.quality_status if gate else "MISSING"
