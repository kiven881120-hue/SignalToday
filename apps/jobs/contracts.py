from django.db import models


class JobStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RUNNING = "RUNNING", "Running"
    RETRYING = "RETRYING", "Retrying"
    PAUSED = "PAUSED", "Paused"
    PAUSED_BUDGET_EXCEEDED = "PAUSED_BUDGET_EXCEEDED", "Paused: budget exceeded"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"
