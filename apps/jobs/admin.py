from django.contrib import admin

from .models import JobRun, JobStep


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    list_display = ("job_type", "status", "idempotency_key", "started_at", "finished_at")
    list_filter = ("status", "job_type")
    search_fields = ("job_type", "idempotency_key")
    readonly_fields = ("created_at",)


@admin.register(JobStep)
class JobStepAdmin(admin.ModelAdmin):
    list_display = ("job_run", "step_name", "status", "started_at", "finished_at")
    list_filter = ("status", "step_name")
    readonly_fields = ("created_at",)
