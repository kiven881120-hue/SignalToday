import logging
import os

from django.db import close_old_connections, connection


logger = logging.getLogger("signaltoday.scheduler")
HEARTBEAT_LOCK_ID = 7_341_057_201


def run_heartbeat():
    close_old_connections()
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [HEARTBEAT_LOCK_ID])
            acquired = cursor.fetchone()[0]
            if not acquired:
                logger.warning("Scheduler heartbeat skipped because the lock is held")
                return False
            cursor.execute("SELECT 1")
            cursor.fetchone()
        logger.info("Scheduler heartbeat completed")
        return True
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [HEARTBEAT_LOCK_ID])
        close_old_connections()


def run_configured_ingestion():
    """Scheduler hook; real feed access remains disabled unless explicitly enabled."""
    if os.getenv("INGESTION_SCHEDULE_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        logger.info("Scheduled ingestion is disabled")
        return False
    from django.core.management import call_command

    call_command("ingest_sources")
    return True
