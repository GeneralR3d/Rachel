"""Calendar / weekly-schedule data access + LangChain tools.

Moved out of repository.py so the schedule lookups live in one place. Everything
Rachel knows about her schedule now flows through the LangChain ``@tool``s bound
to the context_fetcher node: the node calls them (right now / today / other days
/ specific times) and their output is injected into the responder as fetched
context. The responder no longer reads the schedule directly.

The plain async functions (``get_current_activity`` / ``get_day_summary`` …) are
the internal data-access layer the tools are built on. They keep integer
``day_of_week`` (0=Mon … 6=Sun) signatures; the tools wrap them to accept human
day names (e.g. "Saturday"), which are far less error-prone for the model.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa
from langchain_core.tools import tool
from sqlalchemy import select

from app.database import session_scope
from app.models import ScheduleActivity

# Rachel lives in Singapore; "now" for the schedule tools is always SGT.
SGT = timezone(timedelta(hours=8))

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


def _render_overview(idx: int, summary: list[dict], header: str) -> str:
    """Render a day-summary (name/duration/location only) as a bullet list."""
    if not summary:
        return f"Rachel has nothing scheduled on {DAY_NAMES[idx]}."
    lines = [header]
    for a in summary:
        lines.append(f"- {a['name']} ({a['duration_hours']}h) @ {a['location']}")
    return "\n".join(lines)


@tool
async def get_day_overview(day: str) -> str:
    """Get a quick overview of a NAMED day: each activity's name, duration and location only (no details).

    Best for a fast sense of how some *other* day is laid out — e.g. "how packed
    is your Saturday?". For a specific day's full details (who/where/why) use
    get_schedule_for_day; for TODAY's overview use get_today_overview.

    Args:
        day: The day of the week, e.g. "Monday", "Tuesday" … "Sunday".
    """
    idx = _resolve_day(day)
    if idx is None:
        return f"Unrecognised day '{day}'. Use a weekday name like 'Monday'."
    summary = await get_day_summary(idx)
    return _render_overview(idx, summary, f"Overview of Rachel's {DAY_NAMES[idx]}:")


@tool
async def get_today_overview() -> str:
    """Get a quick overview of what Rachel has on TODAY: each activity's name, duration and location only.

    Takes no arguments — it always uses the current (Singapore) date. Use this
    when the conversation is about today's plans and you just need the shape of
    her day. For a named/other day use get_day_overview instead.
    """
    idx = datetime.now(SGT).weekday()
    summary = await get_day_summary(idx)
    return _render_overview(idx, summary, f"Overview of Rachel's today ({DAY_NAMES[idx]}):")


@tool
async def get_activity_now() -> str:
    """Get what Rachel is doing RIGHT NOW, based on the current Singapore time.

    Takes no arguments. Use this when someone asks what she's up to / whether
    she's busy at this moment (e.g. "you free now?", "wyd?"). Returns the single
    activity covering the current hour, or a note that nothing is scheduled.
    """
    now = datetime.now(SGT)
    a = await get_current_activity(now.weekday(), now.hour)
    if a is None:
        return "Rachel has nothing scheduled right now."
    return (
        f"Right now ({DAY_NAMES[now.weekday()]} {now.strftime('%H:%M')}) Rachel is: "
        f"{a['name']} @ {a['location']} ({a['start_hour']:02d}:00–{a['ends_at']}, "
        f"with {a['companions']}): {a['description']} "
        f"[reason: {a['reason']}; event: {a['interesting_event']}]"
    )


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


# All calendar tools exposed to the context_fetcher node. This list is the
# single source of truth for the calendar tools: it feeds the combined tool list
# (CONTEXT_TOOLS in app/services/llm.py) that drives both the LLM's bound tools and
# the prompt's tool listing (via format_tools), so adding a tool here is enough.
CALENDAR_TOOLS = [
    get_activity_now,
    get_today_overview,
    get_schedule_for_day,
    get_day_overview,
    get_activity_at,
]
