from django.contrib import admin
from django.db import transaction

from apps.events.models import ClusterMethod, Event
from apps.events.service import audit_assignment, update_event_bounds
from .models import RawItem


@admin.register(RawItem)
class RawItemAdmin(admin.ModelAdmin):
    list_display = ("title", "event", "source", "published_at", "discovered_at", "status")
    list_filter = ("status", "source", "event")
    search_fields = ("title", "url", "canonical_url", "external_id")
    readonly_fields = ("created_at", "updated_at")

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        old_event_id = None
        if change:
            old_event_id = RawItem.objects.select_for_update().values_list("event_id", flat=True).get(pk=obj.pk)
        super().save_model(request, obj, form, change)
        if change and old_event_id != obj.event_id:
            if obj.event_id:
                Event.objects.filter(pk=obj.event_id).update(
                    cluster_method=ClusterMethod.MANUAL,
                    cluster_confidence=None,
                )
                update_event_bounds(obj.event)
            if old_event_id:
                old_event = Event.objects.filter(pk=old_event_id).first()
                if old_event:
                    update_event_bounds(old_event)
            audit_assignment(
                obj,
                old_event_id,
                obj.event_id,
                method=ClusterMethod.MANUAL,
                confidence=None,
                reason="admin_manual_reassignment",
                actor=request.user,
            )
