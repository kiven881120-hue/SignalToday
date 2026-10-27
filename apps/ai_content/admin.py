from django.contrib import admin, messages

from .models import (
    ClaimCandidate,
    ClaimCandidateEvidenceHint,
    ModelCall,
    ModelPricing,
    PromptVersion,
    ScoreSuggestion,
)
from .services import (
    accept_claim_candidate,
    accept_score_suggestion,
    reject_claim_candidate,
    reject_score_suggestion,
)


@admin.register(PromptVersion)
class PromptVersionAdmin(admin.ModelAdmin):
    list_display = ("purpose", "version", "output_schema_version", "active", "created_at")
    list_filter = ("purpose", "active")
    readonly_fields = ("created_at",)


@admin.register(ModelPricing)
class ModelPricingAdmin(admin.ModelAdmin):
    list_display = ("provider", "model", "version", "input_price", "output_price", "currency", "active")
    list_filter = ("provider", "currency", "active")


@admin.register(ModelCall)
class ModelCallAdmin(admin.ModelAdmin):
    list_display = (
        "provider", "model", "purpose", "event", "status", "input_tokens", "output_tokens", "actual_cost", "created_at",
    )
    list_filter = ("provider", "purpose", "status")
    search_fields = ("model", "purpose", "prompt_version", "schema_version", "analysis_key")
    readonly_fields = (
        "provider", "model", "purpose", "prompt", "prompt_version", "schema_version", "analysis_key",
        "request_started_at", "completed_at", "input_tokens", "output_tokens", "total_tokens",
        "estimated_cost", "actual_cost", "pricing", "latency_ms", "status", "error_code",
        "error_message", "raw_output", "job_run", "job_step", "event", "created_at",
    )


class ReviewActionsMixin:
    actions = ("accept_selected", "reject_selected")

    @admin.action(description="Accept selected proposals")
    def accept_selected(self, request, queryset):
        count = 0
        for item in queryset:
            if item.status == "PROPOSED":
                self.accept_service(item, actor=request.user)
                count += 1
        self.message_user(request, f"Accepted {count} proposal(s).", messages.SUCCESS)

    @admin.action(description="Reject selected proposals")
    def reject_selected(self, request, queryset):
        count = 0
        for item in queryset:
            if item.status == "PROPOSED":
                self.reject_service(item, actor=request.user)
                count += 1
        self.message_user(request, f"Rejected {count} proposal(s).", messages.SUCCESS)


@admin.register(ClaimCandidate)
class ClaimCandidateAdmin(ReviewActionsMixin, admin.ModelAdmin):
    list_display = (
        "claim_text", "event", "accepted_claim", "claim_type", "confidence", "status", "model_call", "created_at",
    )
    list_filter = ("status", "claim_type")
    search_fields = ("claim_text", "normalized_claim", "event__title")
    readonly_fields = (
        "model_call", "normalized_claim", "importance_hint", "importance_reason", "reason",
        "single_source_event", "invalid_evidence_hint_count",
        "raw_output", "status", "review_reason", "accepted_claim", "created_at",
    )
    accept_service = staticmethod(accept_claim_candidate)
    reject_service = staticmethod(reject_claim_candidate)

    @admin.action(description="Accept exactly one proposal")
    def accept_selected(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one ClaimCandidate to accept.", messages.ERROR)
            return
        super().accept_selected(request, queryset)


@admin.register(ClaimCandidateEvidenceHint)
class ClaimCandidateEvidenceHintAdmin(admin.ModelAdmin):
    list_display = (
        "candidate", "proposed_raw_item_id", "raw_item", "hint_type", "status",
        "invalid_reason", "created_at",
    )
    list_filter = ("hint_type", "status", "invalid_reason")
    search_fields = ("candidate__claim_text", "quote")
    readonly_fields = (
        "candidate", "raw_item", "proposed_raw_item_id", "quote", "normalized_quote", "hint_type", "status",
        "invalid_reason", "created_at",
    )


@admin.register(ScoreSuggestion)
class ScoreSuggestionAdmin(ReviewActionsMixin, admin.ModelAdmin):
    list_display = (
        "event", "importance_score", "credibility_score", "user_relevance_score",
        "information_gain_score", "freshness_score", "long_term_impact_score", "status", "created_at",
    )
    list_filter = ("status",)
    search_fields = ("event__title", "overall_reason")
    readonly_fields = ("model_call", "status", "accepted_score", "created_at")
    accept_service = staticmethod(accept_score_suggestion)
    reject_service = staticmethod(reject_score_suggestion)
