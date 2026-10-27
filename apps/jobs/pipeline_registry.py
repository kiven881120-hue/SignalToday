from dataclasses import dataclass
from datetime import time


PIPELINE_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_seconds: float
    retryable_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PipelineStage:
    name: str
    schedule: time
    dependencies: tuple[str, ...]
    timeout_seconds: int
    retry_policy: RetryPolicy
    enabled: bool = True
    mode: str = "AUTOMATIC"
    executor: str = ""

    @property
    def job_type(self):
        return f"PIPELINE_{self.name.upper()}"

    def idempotency_key(self, business_date):
        return f"pipeline:{business_date.isoformat()}:{self.name.replace('_', '-')}"


NO_RETRY = RetryPolicy(1, 0, 0, 0)
NETWORK_RETRY = RetryPolicy(
    3,
    1,
    30,
    0.5,
    ("TIMEOUT", "NETWORK_ERROR", "HTTP_429", "HTTP_5XX", "DEPENDENCY_UNAVAILABLE"),
)


PIPELINE_STAGES = (
    PipelineStage(
        "source_ingestion", time(5, 30), (), 1800, NETWORK_RETRY,
        executor="apps.jobs.pipeline_stages.source_ingestion",
    ),
    PipelineStage(
        "event_clustering", time(5, 45), ("source_ingestion",), 1200, NO_RETRY,
        executor="apps.jobs.pipeline_stages.event_clustering",
    ),
    PipelineStage(
        "claim_analysis", time(5, 55), ("event_clustering",), 1800, NETWORK_RETRY,
        executor="apps.jobs.pipeline_stages.claim_analysis",
    ),
    PipelineStage(
        "claim_evaluation", time(6, 15), ("claim_analysis",), 900, NO_RETRY,
        executor="apps.jobs.pipeline_stages.claim_evaluation",
    ),
    PipelineStage(
        "importance_review", time(6, 25), ("claim_evaluation",), 3600, NO_RETRY,
        mode="MANUAL_GATE", executor="apps.jobs.pipeline_stages.importance_review_gate",
    ),
    PipelineStage(
        "scoring", time(6, 35), ("importance_review",), 900, NO_RETRY,
        executor="apps.jobs.pipeline_stages.scoring",
    ),
    PipelineStage(
        "article_generation", time(6, 35), ("scoring",), 1800, NETWORK_RETRY,
        executor="apps.jobs.pipeline_stages.article_generation",
    ),
    PipelineStage(
        "article_validation", time(6, 50), ("article_generation",), 900, NO_RETRY,
        executor="apps.jobs.pipeline_stages.article_validation",
    ),
    PipelineStage(
        "article_review_gate", time(7, 44), ("article_validation",), 300, NO_RETRY,
        mode="MANUAL_GATE", executor="apps.jobs.pipeline_stages.article_review_gate",
    ),
    PipelineStage(
        "publish_gate", time(7, 45), ("article_review_gate",), 900, NO_RETRY,
        mode="MANUAL_GATE", executor="apps.jobs.pipeline_stages.publish_gate",
    ),
    PipelineStage(
        "daily_digest", time(8, 0), ("publish_gate",), 300, NO_RETRY,
        executor="apps.jobs.pipeline_stages.daily_digest",
    ),
    PipelineStage(
        "public_health_check", time(8, 5), ("daily_digest",), 300, NETWORK_RETRY,
        executor="apps.jobs.pipeline_stages.public_health_check",
    ),
)

PIPELINE_REGISTRY = {stage.name: stage for stage in PIPELINE_STAGES}


def validate_registry(stages=PIPELINE_STAGES):
    names = [stage.name for stage in stages]
    if len(names) != len(set(names)):
        raise ValueError("Pipeline stage names must be unique")
    known = set()
    for stage in stages:
        unknown = set(stage.dependencies) - set(names)
        if unknown:
            raise ValueError(f"Unknown dependencies for {stage.name}: {sorted(unknown)}")
        if not set(stage.dependencies) <= known:
            raise ValueError(f"Dependencies for {stage.name} must appear earlier in the registry")
        if stage.timeout_seconds < 1 or stage.retry_policy.max_attempts < 1:
            raise ValueError(f"Invalid execution policy for {stage.name}")
        known.add(stage.name)
    return True


validate_registry()
