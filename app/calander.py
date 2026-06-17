"""Calendar / weekly-schedule data access + LangChain tools.

Moved out of repository.py so the schedule lookups live in one place and can be
exposed two ways:

- as plain async functions (``get_current_activity`` / ``get_day_summary`` …),
  used by the responder's direct "what am I doing right now / today" reads, and
- as LangChain ``@tool``s, bound to the context_fetcher node so the LLM can pull
  *other* days / specific activities into the responder's context on demand.

The plain functions keep integer ``day_of_week`` (0=Mon … 6=Sun) signatures so
the existing callers in llm.py change as little as possible; the tools accept
human day names (e.g. "Saturday") which are far less error-prone for the model.
"""

from typing import Optional

import sqlalchemy as sa
from langchain_core.tools import tool
from sqlalchemy import select

from app.database import session_scope
from app.models import ScheduleActivity

# 0=Mon … 6=Sun, matching datetime.weekday().
DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
_DAY_NAME_TO_INDEX = {name.lower(): i for i, name in enumerate(DAY_NAMES)}


def _resolve_day(day: str) -> Optional[int]:
    """Map a human day name ("Mon", "monday", "Saturday") to 0..6, or None."""
    if day is None:
        return None
    key = day.strip().lower()
    if key in _DAY_NAME_TO_INDEX:
        return _DAY_NAME_TO_INDEX[key]
    # accept 3-letter abbreviations too
    for name, idx in _DAY_NAME_TO_INDEX.items():
        if name.startswith(key) or key.startswith(name[:3]):
            return idx
    return None


def _activity_to_dict(a: ScheduleActivity, partial: bool = False) -> dict:
    if partial:
        return {
            "name": a.name,
            "duration_hours": a.duration_hours,
            "location": a.location,
        }
    return {
        "day_of_week": a.day_of_week,
        "start_hour": a.start_hour,
        "name": a.name,
        "description": a.description,
        "location": a.location,
        "duration_hours": a.duration_hours,
        "ends_at": f"{(a.start_hour + a.duration_hours) % 24:02d}:00",
        "companions": a.companions,
        "reason": a.reason,
        "interesting_event": a.interesting_event,
    }


# --- plain async data-access functions ------------------------------------


async def get_current_activity(day_of_week: int, hour: int) -> Optional[dict]:
    """Return the activity covering the given hour of the given day, if any.

    Also checks the previous day for activities that start late and run past
    midnight (e.g. start_hour=23, duration_hours=8 covers 23:00-07:00).
    """
    previous_day = (day_of_week - 1) % 7
    async with session_scope() as session:
        activity = await session.scalar(
            select(ScheduleActivity).where(
                sa.or_(
                    sa.and_(
                        ScheduleActivity.day_of_week == day_of_week,
                        ScheduleActivity.start_hour <= hour,
                        ScheduleActivity.start_hour + ScheduleActivity.duration_hours > hour,
                    ),
                    sa.and_(
                        ScheduleActivity.day_of_week == previous_day,
                        ScheduleActivity.start_hour + ScheduleActivity.duration_hours > 24,
                        ScheduleActivity.start_hour + ScheduleActivity.duration_hours - 24 > hour,
                    ),
                )
            )
        )
    return _activity_to_dict(activity) if activity is not None else None


async def get_day_summary(day_of_week: int) -> list[dict]:
    """Return only name, duration_hours and location for the day's activities, ordered by start time."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ScheduleActivity)
                .where(ScheduleActivity.day_of_week == day_of_week)
                .order_by(ScheduleActivity.start_hour)
            )
        ).scalars().all()
    return [_activity_to_dict(a, partial=True) for a in rows]


async def get_day_activities(day_of_week: int) -> list[dict]:
    """Return the FULL detail of every activity on a day, ordered by start time.

    Unlike get_day_summary (name/duration/location only) this includes
    description, companions, reason and interesting_event for each activity.
    """
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ScheduleActivity)
                .where(ScheduleActivity.day_of_week == day_of_week)
                .order_by(ScheduleActivity.start_hour)
            )
        ).scalars().all()
    return [_activity_to_dict(a) for a in rows]


# --- LangChain tools (bound to the context_fetcher node) ------------------


@tool
async def get_schedule_for_day(day: str) -> str:
    """Get Rachel's FULL schedule for a given day of the week.

    Use this to find out everything Rachel is doing on a particular day —
    every activity with its time, location, who she's with, why, and any
    interesting event. Useful when someone asks about her plans for a specific
    day (e.g. "what are you doing this Saturday?").

    Args:
        day: The day of the week, e.g. "Monday", "Tuesday" … "Sunday".
    """
    idx = _resolve_day(day)
    if idx is None:
        return f"Unrecognised day '{day}'. Use a weekday name like 'Monday'."
    activities = await get_day_activities(idx)
    if not activities:
        return f"Rachel has nothing scheduled on {DAY_NAMES[idx]}."
    lines = [f"Rachel's full schedule for {DAY_NAMES[idx]}:"]
    for a in activities:
        lines.append(
            f"- {a['start_hour']:02d}:00–{a['ends_at']} {a['name']} @ {a['location']} "
            f"(with {a['companions']}): {a['description']} "
            f"[reason: {a['reason']}; event: {a['interesting_event']}]"
        )
    return "\n".join(lines)


@tool
async def get_day_overview(day: str) -> str:
    """Get a brief overview of Rachel's day: just the activity names, durations and locations.

    Use this for a quick sense of how a day is laid out, without the full
    detail. For everything about a day, use get_schedule_for_day instead.

    Args:
        day: The day of the week, e.g. "Monday", "Tuesday" … "Sunday".
    """
    idx = _resolve_day(day)
    if idx is None:
        return f"Unrecognised day '{day}'. Use a weekday name like 'Monday'."
    summary = await get_day_summary(idx)
    if not summary:
        return f"Rachel has nothing scheduled on {DAY_NAMES[idx]}."
    lines = [f"Overview of Rachel's {DAY_NAMES[idx]}:"]
    for a in summary:
        lines.append(f"- {a['name']} ({a['duration_hours']}h) @ {a['location']}")
    return "\n".join(lines)


@tool
async def get_activity_at(day: str, hour: int) -> str:
    """Get the single activity Rachel is doing at a specific day and hour.

    Use this to answer "what are you doing on <day> at <hour>?" — it returns
    the one activity covering that time slot (handling activities that run past
    midnight from the previous day).

    Args:
        day: The day of the week, e.g. "Monday" … "Sunday".
        hour: The hour of the day in 24h format, 0–23.
    """
    idx = _resolve_day(day)
    if idx is None:
        return f"Unrecognised day '{day}'. Use a weekday name like 'Monday'."
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        return "hour must be an integer between 0 and 23."
    a = await get_current_activity(idx, hour)
    if a is None:
        return f"Rachel has nothing scheduled on {DAY_NAMES[idx]} at {hour:02d}:00."
    return (
        f"On {DAY_NAMES[idx]} at {hour:02d}:00 Rachel is: {a['name']} @ {a['location']} "
        f"({a['start_hour']:02d}:00–{a['ends_at']}, with {a['companions']}): "
        f"{a['description']} [reason: {a['reason']}; event: {a['interesting_event']}]"
    )


# All calendar tools exposed to the context_fetcher node.
CALENDAR_TOOLS = [get_schedule_for_day, get_day_overview, get_activity_at]
