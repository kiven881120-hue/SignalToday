import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.jobs.pipeline import _local_now, plan_pipeline, scheduled_stage
from apps.jobs.pipeline_registry import PIPELINE_STAGES, PIPELINE_TIMEZONE
from apps.jobs.scheduler import run_heartbeat


class Command(BaseCommand):
    help = "Run the dedicated SignalToday scheduler process."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--date", help="Pipeline business date in YYYY-MM-DD format")
        parser.add_argument("--at", help="Evaluate schedules at this ISO-8601 timestamp")

    def handle(self, *args, **options):
        if options["dry_run"]:
            if not options["once"]:
                raise CommandError("--dry-run requires --once")
            try:
                now = datetime.fromisoformat(options["at"]) if options["at"] else None
                business_date = date.fromisoformat(options["date"]) if options["date"] else (
                    _local_now(now).date() if now else None
                )
            except ValueError as exc:
                raise CommandError("--date or --at has an invalid ISO format") from exc
            if business_date is None:
                from django.utils import timezone

                business_date = timezone.now().astimezone(ZoneInfo(PIPELINE_TIMEZONE)).date()
            self.stdout.write(json.dumps({
                "dry_run": True,
                "pipeline_enabled": bool(settings.SCHEDULER_PIPELINE_ENABLED),
                "business_date": business_date.isoformat(),
                "timezone": PIPELINE_TIMEZONE,
                "stages": plan_pipeline(business_date, now=now),
            }, ensure_ascii=False, sort_keys=True))
            return
        if options["once"]:
            if not run_heartbeat():
                raise CommandError("Scheduler heartbeat lock is already held")
            self.stdout.write(self.style.SUCCESS("scheduler heartbeat: ok"))
            return

        interval = options["interval"]
        if interval < 1:
            raise CommandError("--interval must be at least 1 second")

        scheduler = BlockingScheduler(timezone=ZoneInfo(PIPELINE_TIMEZONE))
        scheduler.add_job(
            run_heartbeat,
            "interval",
            seconds=interval,
            id="stage1-heartbeat",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(interval, 30),
        )
        if settings.SCHEDULER_PIPELINE_ENABLED:
            for stage in PIPELINE_STAGES:
                if not stage.enabled:
                    continue
                scheduler.add_job(
                    scheduled_stage,
                    "cron",
                    args=[stage.name],
                    hour=stage.schedule.hour,
                    minute=stage.schedule.minute,
                    timezone=ZoneInfo(PIPELINE_TIMEZONE),
                    id=f"daily-pipeline-{stage.name}",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                )
        self.stdout.write("SignalToday scheduler started")
        self.stdout.write(
            f"Daily Pipeline enabled={settings.SCHEDULER_PIPELINE_ENABLED} "
            f"timezone={PIPELINE_TIMEZONE}"
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.stdout.write("SignalToday scheduler stopped")
