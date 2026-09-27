"""In-process APScheduler service for persisted proactive outreach definitions."""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import repository
from app.telegram.client import send_proactive_message

logger = logging.getLogger("rachel.proactive")

_scheduler: AsyncIOScheduler | None = None
_schedule_locks: dict[int, asyncio.Lock] = {}
_active_tasks: set[asyncio.Task] = set()
_accepting_runs = False


def _job_id(schedule_id: int) -> str:
    return f"proactive-schedule-{schedule_id}"


def build_trigger(cron_expression: str, timezone_name: str) -> CronTrigger:
    """Validate and construct a five-field cron trigger in an IANA timezone."""
    zone = ZoneInfo(timezone_name)
    return CronTrigger.from_crontab(cron_expression, timezone=zone)


def _add_or_replace_job(schedule: dict) -> None:
    if _scheduler is None:
        return
    job_id = _job_id(schedule["id"])
    if not schedule["enabled"]:
        remove_schedule_job(schedule["id"])
        return
    _scheduler.add_job(
        _scheduled_run,
        trigger=build_trigger(schedule["cron_expression"], schedule["timezone"]),
        args=[schedule["id"]],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )


async def start_proactive_scheduler() -> None:
    """Rebuild all enabled jobs from Postgres and begin scheduling future runs."""
    global _scheduler, _accepting_runs
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop(), timezone="UTC")
    for schedule in await repository.get_proactive_schedules(enabled_only=True):
        _add_or_replace_job(schedule)
    _accepting_runs = True
    _scheduler.start()
    logger.info("Proactive scheduler started with %d job(s)", len(_scheduler.get_jobs()))


async def stop_proactive_scheduler() -> None:
    """Stop new occurrences, await active executions, then tear down the scheduler."""
    global _scheduler, _accepting_runs
    _accepting_runs = False
    scheduler = _scheduler
    if scheduler is None:
        return
    scheduler.pause()
    if _active_tasks:
        await asyncio.gather(*list(_active_tasks), return_exceptions=True)
    scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("Proactive scheduler stopped")


def sync_schedule_job(schedule: dict) -> None:
    """Apply one create/update/enable change to the in-memory scheduler."""
    _add_or_replace_job(schedule)


def remove_schedule_job(schedule_id: int) -> None:
    if _scheduler is None:
        return
    job = _scheduler.get_job(_job_id(schedule_id))
    if job is not None:
        _scheduler.remove_job(job.id)


def next_run_at(schedule_id: int) -> datetime | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(_job_id(schedule_id))
    return job.next_run_time if job is not None else None


def is_schedule_running(schedule_id: int) -> bool:
    return _schedule_locks.setdefault(schedule_id, asyncio.Lock()).locked()


async def _scheduled_run(schedule_id: int) -> None:
    await run_schedule(schedule_id)


async def run_schedule(schedule_id: int) -> bool:
    """Run one schedule through the shared generation/send path.

    Returns False if the service is stopping, the row vanished, or this schedule
    already has a process-local execution in progress.
    """
    if not _accepting_runs:
        return False
    lock = _schedule_locks.setdefault(schedule_id, asyncio.Lock())
    if lock.locked():
        return False

    task = asyncio.current_task()
    if task is not None:
        _active_tasks.add(task)
    try:
        async with lock:
            schedule = await repository.get_proactive_schedule(schedule_id)
            if schedule is None:
                return False
            ran_at = datetime.now(timezone.utc)
            await repository.set_proactive_schedule_status(
                schedule_id, "running", ran_at=ran_at
            )
            try:
                await send_proactive_message(
                    schedule["chat_id"], schedule["intent"], schedule_id
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                error = f"{type(exc).__name__}: {exc}"[:4000]
                await repository.set_proactive_schedule_status(
                    schedule_id, "failed", error=error
                )
                logger.exception("Proactive schedule %s failed", schedule_id)
                return True
            await repository.set_proactive_schedule_status(
                schedule_id, "succeeded", error=None
            )
            return True
    finally:
        if task is not None:
            _active_tasks.discard(task)
