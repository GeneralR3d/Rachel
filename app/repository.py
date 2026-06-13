"""Async data-access layer — a port of the original Reference/app/db.py.

Function names and signatures are kept identical so the Telethon handlers and
HTTP routes change as little as possible. Each function opens its own session
(mirroring the original "connect per call" style) via ``session_scope``.
"""

import time
from typing import Optional, Union

import sqlalchemy as sa
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import session_scope
from app.models import (
    History,
    PersonalityTrait,
    ScheduleActivity,
    Summary,
    SystemPrompt,
    User,
    UserFactsPreferences,
)
from app.prompts import DEFAULT_TRAITS, SUMMARIZER_SYSTEM_PROMPT, RESPONDER_SYSTEM_PROMPT
from app.schedule_data import DEFAULT_SCHEDULE

# Cached system prompts (mirrors the original module-level global cache).
_responder_system_prompt: Optional[str] = None
_summarizer_system_prompt: Optional[str] = None

# Cached active trait prompt block, refreshed at most every TRAIT_CACHE_TTL seconds.
TRAIT_CACHE_TTL = 5 * 60    # 5 min
_active_trait_prompts_cache: Optional[str] = None
_active_trait_prompts_cache_time: float = 0.0


async def ensure_traits_seeded() -> None:
    """Upsert default personality traits by name.

    Inserts any trait missing from the table, and refreshes
    sort_order/low_prompt/medium_prompt/high_prompt for existing ones so
    edits to DEFAULT_TRAITS take effect on restart. current_value is left
    untouched so admin-tuned levels survive prompt-text edits.
    """
    async with session_scope() as session:
        for trait in DEFAULT_TRAITS:
            trait_data = {k: v for k, v in trait.items() if k != "default_value"}
            stmt = pg_insert(PersonalityTrait).values(
                **trait_data, current_value=trait.get("default_value", "medium")
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[PersonalityTrait.name],
                set_={
                    "sort_order": stmt.excluded.sort_order,
                    "low_prompt": stmt.excluded.low_prompt,
                    "medium_prompt": stmt.excluded.medium_prompt,
                    "high_prompt": stmt.excluded.high_prompt,
                },
            )
            await session.execute(stmt)
    _invalidate_trait_prompt_cache()


# --- personality traits --------------------------------------------------


async def get_traits() -> list[dict]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(PersonalityTrait).order_by(PersonalityTrait.sort_order)
            )
        ).scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "sort_order": t.sort_order,
            "low_prompt": t.low_prompt,
            "medium_prompt": t.medium_prompt,
            "high_prompt": t.high_prompt,
            "current_value": t.current_value,
        }
        for t in rows
    ]


def _invalidate_trait_prompt_cache() -> None:
    global _active_trait_prompts_cache, _active_trait_prompts_cache_time
    _active_trait_prompts_cache = None
    _active_trait_prompts_cache_time = 0.0


async def set_trait_value(trait_id: int, value: str) -> bool:
    """Set current_value for a trait. Returns False if the trait does not exist."""
    async with session_scope() as session:
        result = await session.execute(
            update(PersonalityTrait)
            .where(PersonalityTrait.id == trait_id)
            .values(current_value=value)
        )
    if result.rowcount > 0:
        _invalidate_trait_prompt_cache()
    return result.rowcount > 0


async def reset_traits() -> None:
    """Set all traits back to their individual default values."""
    defaults = {t["name"]: t.get("default_value", "medium") for t in DEFAULT_TRAITS}
    async with session_scope() as session:
        for name, value in defaults.items():
            await session.execute(
                update(PersonalityTrait)
                .where(PersonalityTrait.name == name)
                .values(current_value=value)
            )
    _invalidate_trait_prompt_cache()


async def get_active_trait_prompts() -> str:
    """Assemble all active trait prompts into a single block for the system prompt.

    Cached for TRAIT_CACHE_TTL seconds since this is read on every message.
    """
    global _active_trait_prompts_cache, _active_trait_prompts_cache_time

    now = time.monotonic()
    if _active_trait_prompts_cache is not None and (now - _active_trait_prompts_cache_time) < TRAIT_CACHE_TTL:
        return _active_trait_prompts_cache

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    PersonalityTrait.name,
                    sa.case(
                        (PersonalityTrait.current_value == "low", PersonalityTrait.low_prompt),
                        (PersonalityTrait.current_value == "medium", PersonalityTrait.medium_prompt),
                        (PersonalityTrait.current_value == "high", PersonalityTrait.high_prompt),
                    ).label("active_prompt"),
                ).order_by(PersonalityTrait.sort_order)
            )
        ).all()
    lines = [f"- {r.name}: {r.active_prompt}" for r in rows]
    _active_trait_prompts_cache = "\n".join(lines)
    _active_trait_prompts_cache_time = now
    return _active_trait_prompts_cache


async def ensure_schedule_seeded() -> None:
    """Upsert the weekly schedule from schedule_data.py on every startup.

    schedule_data.py is the source of truth — edits there (including new
    entries appended after the first run) take effect on next restart, keyed
    on the (day_of_week, start_hour) unique constraint.
    """
    async with session_scope() as session:
        for activity in DEFAULT_SCHEDULE:
            stmt = (
                pg_insert(ScheduleActivity)
                .values(**activity)
                .on_conflict_do_update(
                    index_elements=[ScheduleActivity.day_of_week, ScheduleActivity.start_hour],
                    set_={k: v for k, v in activity.items() if k not in ("day_of_week", "start_hour")},
                )
            )
            await session.execute(stmt)


# --- weekly schedule ------------------------------------------------------


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


async def ensure_system_prompts_seeded() -> None:
    """Upsert system prompts from prompts.py on every startup.

    prompts.py is the source of truth — edits there take effect on next restart.
    Bot-side edits via /set_*_system_prompt survive until the next restart.
    """
    global _responder_system_prompt, _summarizer_system_prompt
    async with session_scope() as session:
        existing = await session.scalar(select(SystemPrompt.id).limit(1))
        if existing is None:
            session.add(
                SystemPrompt(
                    responder_system_prompt=RESPONDER_SYSTEM_PROMPT,
                    summarizer_system_prompt=SUMMARIZER_SYSTEM_PROMPT,
                )
            )
        else:
            await session.execute(
                update(SystemPrompt).values(
                    responder_system_prompt=RESPONDER_SYSTEM_PROMPT,
                    summarizer_system_prompt=SUMMARIZER_SYSTEM_PROMPT,
                )
            )
    # Invalidate in-memory cache so first read picks up the new values
    _responder_system_prompt = None
    _summarizer_system_prompt = None


# --- system prompt -------------------------------------------------------


async def get_responder_system_prompt() -> str:
    global _responder_system_prompt
    if _responder_system_prompt:
        return _responder_system_prompt

    async with session_scope() as session:
        _responder_system_prompt = await session.scalar(
            select(SystemPrompt.responder_system_prompt).limit(1)
        )
    return _responder_system_prompt


async def set_responder_system_prompt(new_system_prompt: str) -> None:
    global _responder_system_prompt
    _responder_system_prompt = new_system_prompt

    async with session_scope() as session:
        await session.execute(
            update(SystemPrompt).values(responder_system_prompt=new_system_prompt)
        )


async def get_summarizer_system_prompt() -> str:
    global _summarizer_system_prompt
    if _summarizer_system_prompt:
        return _summarizer_system_prompt

    async with session_scope() as session:
        _summarizer_system_prompt = await session.scalar(
            select(SystemPrompt.summarizer_system_prompt).limit(1)
        )
    return _summarizer_system_prompt


async def set_summarizer_system_prompt(new_summarizer_system_prompt: str) -> None:
    global _summarizer_system_prompt
    _summarizer_system_prompt = new_summarizer_system_prompt

    async with session_scope() as session:
        await session.execute(
            update(SystemPrompt).values(summarizer_system_prompt=new_summarizer_system_prompt)
        )


# --- users ---------------------------------------------------------------


async def upsert_user(
    telegram_user_id: int,
    first_name: Optional[str],
    last_name: Optional[str],
    username: Optional[str],
) -> None:
    """Insert or update a Telegram user's profile info."""
    async with session_scope() as session:
        stmt = (
            pg_insert(User)
            .values(
                telegram_user_id=telegram_user_id,
                first_name=first_name,
                last_name=last_name,
                username=username,
            )
            .on_conflict_do_update(
                index_elements=[User.telegram_user_id],
                set_={
                    "first_name": first_name,
                    "last_name": last_name,
                    "username": username,
                    "updated_at": func.now(),
                },
            )
        )
        await session.execute(stmt)


# --- history -------------------------------------------------------------


async def get_all_users() -> list[dict]:
    """Return all known users."""
    async with session_scope() as session:
        rows = (await session.execute(select(User).order_by(User.telegram_user_id))).scalars().all()
    return [
        {
            "telegram_user_id": u.telegram_user_id,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "username": u.username,
            "created_at": u.created_at,
            "updated_at": u.updated_at,
        }
        for u in rows
    ]


async def get_all_chats() -> list[dict]:
    """Return all chat_ids with their message counts."""
    stmt = (
        select(History.chat_id, func.count(History.telegram_message_id).label("message_count"))
        .group_by(History.chat_id)
        .order_by(History.chat_id)
    )
    async with session_scope() as session:
        rows = (await session.execute(stmt)).all()
    return [{"chat_id": r.chat_id, "message_count": r.message_count} for r in rows]


async def get_history(chat_id: int, count: int = -1) -> list[dict]:
    """Return history rows in ascending message order.

    count: number of most-recent rows to return (-1 for all).
    """
    # Prioritize first name, better for summary and fact extraction
    # Might cause conflicts if 2 ppl in same chat have exact same first name, which is unlikely
    display_name = func.coalesce(
        User.first_name,
        User.username,
        sa.cast(History.sender_user_id, sa.Text),
    ).label("sender")

    base = (
        select(History.chat_id, History.sender_user_id, History.content, History.reason, History.telegram_message_id, display_name)
        .outerjoin(User, History.sender_user_id == User.telegram_user_id)
        .where(History.chat_id == chat_id)
    )

    if count != -1:
        subq = base.order_by(History.telegram_message_id.desc()).limit(count).subquery()
        stmt = select(subq).order_by(subq.c.telegram_message_id.asc())
    else:
        stmt = base.order_by(History.telegram_message_id.asc())

    async with session_scope() as session:
        rows = (await session.execute(stmt)).all()

    return [
        {
            "sender": r.sender,
            "sender_user_id": r.sender_user_id,
            "content": r.content,
            "reason": r.reason,
            "telegram_message_id": r.telegram_message_id,
            "chat_id": r.chat_id,
        }
        for r in rows
    ]


async def add_history(
    chat_id: int, sender_user_id: int, content: str, telegram_message_id: int
) -> None:
    async with session_scope() as session:
        await session.execute(
            pg_insert(History).values(
                chat_id=chat_id,
                sender_user_id=sender_user_id,
                content=content,
                telegram_message_id=telegram_message_id,
            )
        )


async def add_history_batch(
    chat_ids: list[int],
    sender_user_ids: list[int],
    contents: list[str],
    telegram_message_ids: list[int],
    reasons: list[Optional[str]] | None = None,
) -> None:
    """Insert multiple history entries. Works for a single message too.

    reasons is optional and parallel to the other lists; entries are None for
    inbound user messages, which have no responder reason.
    """
    if reasons is None:
        reasons = [None] * len(chat_ids)
    if not len(chat_ids) == len(sender_user_ids) == len(contents) == len(telegram_message_ids) == len(reasons):
        raise ValueError(
            "chat_ids, sender_user_ids, contents, telegram_message_ids, and reasons must be lists of the same length"
        )

    rows = [
        {
            "chat_id": chat_ids[i],
            "sender_user_id": sender_user_ids[i],
            "content": contents[i],
            "telegram_message_id": telegram_message_ids[i],
            "reason": reasons[i],
        }
        for i in range(len(chat_ids))
    ]

    if not rows:
        return

    async with session_scope() as session:
        await session.execute(pg_insert(History).on_conflict_do_nothing(), rows)


async def clear_history(chat_id: int) -> None:
    async with session_scope() as session:
        await session.execute(delete(History).where(History.chat_id == chat_id))


async def get_history_min_id(chat_id: int) -> int:
    """Min telegram_message_id stored for a chat, used to incrementally update history."""
    async with session_scope() as session:
        out = await session.scalar(
            select(func.min(History.telegram_message_id)).where(History.chat_id == chat_id)
        )
    # return 0 if no min id (no messages stored yet)
    return out if out else 0


async def rewrite_history(chat_id: int, parsed_history: list[dict]) -> None:
    async with session_scope() as session:
        await session.execute(delete(History).where(History.chat_id == chat_id))
        for item in parsed_history:
            await session.execute(
                pg_insert(History).values(
                    telegram_message_id=item["telegram_message_id"],
                    chat_id=chat_id,
                    sender_user_id=item["sender_user_id"],
                    content=item["content"],
                )
            )


# --- summary -------------------------------------------------------------


async def get_summary(chat_id: int) -> Union[str, None]:
    async with session_scope() as session:
        return await session.scalar(
            select(Summary.summary).where(Summary.chat_id == chat_id)
        )


async def set_summary(chat_id: int, summary: str) -> None:
    """Upsert the summary for a chat."""
    async with session_scope() as session:
        stmt = pg_insert(Summary).values(chat_id=chat_id, summary=summary)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Summary.chat_id], set_={"summary": summary}
        )
        await session.execute(stmt)


async def delete_summary(chat_id: int) -> None:
    async with session_scope() as session:
        await session.execute(delete(Summary).where(Summary.chat_id == chat_id))


# --- user facts / preferences ---------------------------------------------


async def get_user_facts(user_id: int) -> str:
    """Return the raw facts/preferences text for a user, or "" if none stored yet."""
    async with session_scope() as session:
        facts = await session.scalar(
            select(UserFactsPreferences.facts).where(UserFactsPreferences.user_id == user_id)
        )
    return facts or ""


async def get_user_facts_batch(user_ids: list[int]) -> dict[int, str]:
    """Return facts/preferences text for many users in a single query.

    Keyed by user_id; users with no stored facts are simply absent from the dict.
    """
    if not user_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(UserFactsPreferences.user_id, UserFactsPreferences.facts)
                .where(UserFactsPreferences.user_id.in_(user_ids))
            )
        ).all()
    return {r.user_id: r.facts for r in rows if r.facts}


async def set_user_facts(user_id: int, facts: str) -> None:
    """Upsert the full facts/preferences text for a user."""
    async with session_scope() as session:
        stmt = pg_insert(UserFactsPreferences).values(user_id=user_id, facts=facts)
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserFactsPreferences.user_id],
            set_={"facts": facts, "updated_at": func.now()},
        )
        await session.execute(stmt)


async def delete_user_facts(user_id: int) -> None:
    async with session_scope() as session:
        await session.execute(
            delete(UserFactsPreferences).where(UserFactsPreferences.user_id == user_id)
        )
