from django.db import models

from .contracts import JobStatus


class JobRun(models.Model):
    job_type = models.CharField(max_length=100)
    status = models.CharField(max_length=32, choices=JobStatus.choices, default=JobStatus.PENDING, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class JobStep(models.Model):
    job_run = models.ForeignKey(JobRun, on_delete=models.CASCADE, related_name="steps")
    step_name = models.CharField(max_length=100)
    status = models.CharField(max_length=32, choices=JobStatus.choices, default=JobStatus.PENDING, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    input_summary = models.JSONField(default=dict, blank=True)
    output_summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job_run", "step_name"], name="jobrun_step_name_uniq")]
