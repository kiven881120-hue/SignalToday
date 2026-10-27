from django.contrib import admin
from django.db.models import OuterRef, Subquery

from apps.ingestion.models import RawItem
from apps.claims.models import Claim
from apps.scoring.models import Score

from .models import Event


class RawItemInline(admin.TabularInline):
    model = RawItem
    fields = ("title", "source", "published_at", "discovered_at")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True


class ClaimInline(admin.TabularInline):
    model = Claim
    fields = ("statement", "claim_type", "verification_status", "importance")
    readonly_fields = ("claim_type", "verification_status", "importance")
    extra = 0
    show_change_link = True


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = (
        "title", "status", "first_seen_at", "last_seen_at", "cluster_method",
        "cluster_confidence", "raw_items_total", "sources_total", "independent_sources_total",
        "current_score", "current_recommendation",
    )
    list_filter = ("status", "cluster_method")
    search_fields = ("title", "normalized_title", "summary", "cluster_key")
    readonly_fields = ("created_at", "updated_at")
    inlines = (RawItemInline, ClaimInline)

    def get_queryset(self, request):
        current = Score.objects.filter(event=OuterRef("pk"), rule_version__active=True).order_by("-updated_at", "-pk")
        return super().get_queryset(request).annotate(
            _current_score=Subquery(current.values("final_score")[:1]),
            _current_recommendation=Subquery(current.values("recommendation_level")[:1]),
        )

    @admin.display(description="Raw items")
    def raw_items_total(self, obj):
        return obj.raw_item_count

    @admin.display(description="Sources")
    def sources_total(self, obj):
        return obj.unique_source_count

    @admin.display(description="Independent sources")
    def independent_sources_total(self, obj):
        return obj.independent_source_count

    @admin.display(description="Current score", ordering="_current_score")
    def current_score(self, obj):
        return obj._current_score

    @admin.display(description="Recommendation", ordering="_current_recommendation")
    def current_recommendation(self, obj):
        return obj._current_recommendation
