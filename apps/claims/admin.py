from django.contrib import admin
from django.db.models import Count

from .models import Claim, ClaimEvidence


class ClaimEvidenceInline(admin.TabularInline):
    model = ClaimEvidence
    fields = ("raw_item", "source", "evidence_text", "stance", "evidence_type", "captured_at")
    readonly_fields = ("source", "stance")
    extra = 0
    show_change_link = True


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("statement", "event", "verification_status", "claim_type", "evidence_count", "importance")
    list_filter = ("claim_type", "verification_status")
    search_fields = ("statement", "normalized_statement")
    readonly_fields = (
        "claim_type", "importance", "verification_status", "normalized_statement", "created_at", "updated_at",
    )
    inlines = (ClaimEvidenceInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_evidence_count=Count("evidence"))

    @admin.display(ordering="_evidence_count", description="Evidence count")
    def evidence_count(self, obj):
        return obj._evidence_count

    def save_model(self, request, obj, form, change):
        obj._audit_context = {"actor": request.user, "reason": "admin_manual_update"}
        super().save_model(request, obj, form, change)


@admin.register(ClaimEvidence)
class ClaimEvidenceAdmin(admin.ModelAdmin):
    list_display = ("claim", "raw_item", "source", "stance", "evidence_type", "captured_at")
    list_filter = ("stance", "evidence_type", "source")
    search_fields = ("claim__statement", "evidence_locator", "evidence_text")
    readonly_fields = ("source", "stance", "normalized_evidence_text", "created_at")
    list_select_related = ("claim", "raw_item", "source")

    def save_model(self, request, obj, form, change):
        obj._audit_context = {"actor": request.user, "reason": "admin_manual_update"}
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        obj._audit_context = {"actor": request.user, "reason": "admin_manual_delete"}
        super().delete_model(request, obj)
