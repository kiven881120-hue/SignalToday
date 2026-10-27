from django.contrib import admin

from .models import Score, ScoreRuleVersion


@admin.register(ScoreRuleVersion)
class ScoreRuleVersionAdmin(admin.ModelAdmin):
    list_display = ("version", "name", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("version", "name")
    readonly_fields = ("created_at",)


@admin.register(Score)
class ScoreAdmin(admin.ModelAdmin):
    list_display = (
        "event", "article", "rule_version", "importance", "credibility", "user_relevance",
        "information_gain", "freshness", "long_term_impact", "final_score",
        "recommendation_level", "created_at",
    )
    list_filter = ("status", "recommendation_level", "rule_version")
    readonly_fields = ("final_score", "recommendation_level", "recommendation_reason", "created_at", "updated_at")
    list_select_related = ("event", "article", "rule_version")

    @admin.display(description="Recommendation reason")
    def recommendation_reason(self, obj):
        return obj.reasons.get("recommendation_reason", "") if obj else ""

    def save_model(self, request, obj, form, change):
        obj._audit_context = {"actor": request.user}
        super().save_model(request, obj, form, change)
