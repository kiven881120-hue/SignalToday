from django.contrib import admin

from .models import (
    Article, ArticleContentValidation, ArticleVersion, ArticleVersionClaim,
    Digest, DigestItem, Image,
)


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = ("event", "status", "risk_level", "published_at", "updated_at")
    list_filter = ("status", "risk_level")
    readonly_fields = (
        "status", "current_version", "published_version", "published_at",
        "corrected_at", "retracted_at", "created_at", "updated_at",
    )


@admin.register(ArticleVersion)
class ArticleVersionAdmin(admin.ModelAdmin):
    list_display = (
        "article", "version_number", "status", "classification", "language", "created_at",
    )
    list_filter = ("status", "classification", "language")
    search_fields = ("title", "one_sentence_summary")
    readonly_fields = ("status", "classification", "classified_by", "classified_at", "created_at")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj and obj.status != "DRAFT":
            fields.extend(ArticleVersion.CONTENT_FIELDS)
            fields.extend(("article", "version_number", "created_by"))
        return tuple(fields)


@admin.register(ArticleVersionClaim)
class ArticleVersionClaimAdmin(admin.ModelAdmin):
    list_display = ("article_version", "claim", "paragraph_key", "display_order")
    list_filter = ("paragraph_key",)
    readonly_fields = ("article_version", "claim", "paragraph_key", "display_order")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ArticleContentValidation)
class ArticleContentValidationAdmin(admin.ModelAdmin):
    list_display = (
        "article_version", "grounding_status", "quality_status", "validated_at",
    )
    list_filter = ("grounding_status", "quality_status")
    readonly_fields = (
        "article_version", "content_hash", "grounding_status",
        "grounding_validator_version", "grounding_result", "quality_status",
        "quality_validator_version", "quality_input", "quality_result",
        "created_at", "validated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    list_display = ("article", "image_type", "status", "license_state", "created_at")
    list_filter = ("image_type", "status", "license_state")
    readonly_fields = ("created_at",)


@admin.register(Digest)
class DigestAdmin(admin.ModelAdmin):
    list_display = ("digest_date", "status", "item_count", "created_at", "updated_at")
    list_filter = ("status",)
    readonly_fields = ("digest_date", "status", "created_at", "updated_at")

    def item_count(self, obj):
        return obj.items.count()

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DigestItem)
class DigestItemAdmin(admin.ModelAdmin):
    list_display = (
        "digest", "article", "article_version", "event", "rank", "category",
        "recommendation_reason",
    )
    list_filter = ("category",)
    readonly_fields = (
        "digest", "article", "article_version", "category", "rank",
        "recommendation_reason", "created_at",
    )

    def event(self, obj):
        return obj.article.event

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
