"""Shared proactive schedule validation and output for HTTP and LangChain."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field


class ProactiveScheduleIn(BaseModel):
    name: str
    chat_id: int
    cron_expression: str
    timezone: str = "Asia/Singapore"
    intent: str
    enabled: bool = True
    repeat_times: int | None = Field(default=None, strict=True, gt=0, le=9223372036854775807)


class ProactiveScheduleOut(ProactiveScheduleIn):
    id: int
    repeat_completed: int
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None = None


async def validated_schedule_data(
    body: ProactiveScheduleIn, resolve_chat: Callable[[int], Awaitable[Any]]
) -> dict:
    """Normalize editable fields and validate the trigger and Telegram access."""
    name = body.name.strip()
    intent = body.intent.strip()
    cron_expression = " ".join(body.cron_expression.split())
    timezone_name = body.timezone.strip()
    if not name:
        raise ValueError("name must not be empty")
    if not intent:
        raise ValueError("intent must not be empty")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid IANA timezone: {timezone_name!r}") from exc
    try:
        CronTrigger.from_crontab(cron_expression, timezone=zone)
    except ValueError as exc:
        raise ValueError(f"Invalid five-field cron expression: {exc}") from exc
    try:
        await resolve_chat(body.chat_id)
    except Exception as exc:
        raise ValueError(f"Rachel cannot access Telegram chat {body.chat_id}: {exc}") from exc
    return {
        "name": name, "chat_id": body.chat_id, "cron_expression": cron_expression,
        "timezone": timezone_name, "intent": intent, "enabled": body.enabled,
        # Omitted on PUT preserves the existing limit; explicit null clears it.
        **({"repeat_times": body.repeat_times} if "repeat_times" in body.model_fields_set else {}),
    }


def schedule_out(row: dict) -> ProactiveScheduleOut:
    # The scheduler imports Telegram, which imports llm and its tool registry.
    from app.services import proactive

    return ProactiveScheduleOut(
        **row, next_run_at=(
            None if not row["enabled"] or proactive.is_schedule_exhausted(row)
            else proactive.next_run_at(row["id"])
        ),
    )
