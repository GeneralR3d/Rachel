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
from app.config import get_settings
from app.models import (
    ActiveModel,
    ChatState,
    History,
    LLMModel,
    PersonalityTrait,
    ScheduleActivity,
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

# The three switchable LLM roles and their corresponding settings fields (used
# as the seed default when a role has no active row yet).
MODEL_ROLES = ("main", "small", "embedding")

# Cached active-model map (role -> model_string). Read on every LLM client build,
# changes rarely — Style-A cache: populated on read, nulled on write/seed.
_active_models_cache: Optional[dict[str, str]] = None


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
# Schedule lookups (get_current_activity / get_day_summary / …) now live in
# app/calander.py, alongside the LangChain tool wrappers built on top of them.


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


# --- llm models ----------------------------------------------------------


def _role_settings_default(role: str) -> str:
    """The .env/config fallback for a role, used when no active row exists yet."""
    settings = get_settings()
    return {
        "main": settings.llm_model,
        "small": settings.llm_small_model,
        "embedding": settings.llm_embedding_model,
    }[role]


def _invalidate_active_models_cache() -> None:
    """Drop the cached active-model map AND rebuild-trigger the LLM clients.

    The client-cache reset lives in app.services.llm; import it lazily so
    repository doesn't take a module-level dependency on llm (llm imports
    repository, so a top-level import here would be a cycle)."""
    global _active_models_cache
    _active_models_cache = None
    try:
        from app.services.llm import invalidate_model_clients

        invalidate_model_clients()
    except Exception:
        # Never let a client-cache reset failure break the DB write that
        # triggered it; the next build simply reads the fresh DB value.
        pass


async def ensure_models_seeded() -> None:
    """Seed the model catalog + active-model rows from config on every startup.

    Catalog: idempotently insert the three configured model strings so the
    dashboard always has at least the current models to pick from. Active rows:
    inserted only if absent (on_conflict_do_nothing), so an admin's runtime
    switch survives restarts rather than being reset to the .env values.
    """
    settings = get_settings()
    async with session_scope() as session:
        for model_string in {
            settings.llm_model,
            settings.llm_small_model,
            settings.llm_embedding_model,
        }:
            await session.execute(
                pg_insert(LLMModel)
                .values(model_string=model_string)
                .on_conflict_do_nothing(index_elements=[LLMModel.model_string])
            )
        for role in MODEL_ROLES:
            await session.execute(
                pg_insert(ActiveModel)
                .values(role=role, model_string=_role_settings_default(role))
                .on_conflict_do_nothing(index_elements=[ActiveModel.role])
            )
    _invalidate_active_models_cache()


async def get_all_models() -> list[dict]:
    """Return the model-string catalog."""
    async with session_scope() as session:
        rows = (
            await session.execute(select(LLMModel).order_by(LLMModel.model_string))
        ).scalars().all()
    return [{"id": m.id, "model_string": m.model_string} for m in rows]


async def add_model(model_string: str) -> dict:
    """Add a model string to the catalog (idempotent). Returns the row."""
    async with session_scope() as session:
        await session.execute(
            pg_insert(LLMModel)
            .values(model_string=model_string)
            .on_conflict_do_nothing(index_elements=[LLMModel.model_string])
        )
        row = (
            await session.execute(
                select(LLMModel).where(LLMModel.model_string == model_string)
            )
        ).scalar_one()
        return {"id": row.id, "model_string": row.model_string}


async def delete_model(model_string: str) -> None:
    """Remove a model string from the catalog. Does not touch active_models."""
    async with session_scope() as session:
        await session.execute(
            delete(LLMModel).where(LLMModel.model_string == model_string)
        )


async def get_active_models() -> dict[str, str]:
    """Return the role -> active-model-string map, falling back to config for any
    role with no active row. Cached (Style A) since it's read on every LLM build."""
    global _active_models_cache
    if _active_models_cache is not None:
        return _active_models_cache

    async with session_scope() as session:
        rows = (await session.execute(select(ActiveModel))).scalars().all()
    active = {r.role: r.model_string for r in rows}
    # Guarantee every role is present so callers can index unconditionally.
    for role in MODEL_ROLES:
        active.setdefault(role, _role_settings_default(role))
    _active_models_cache = active
    return active


async def set_active_model(role: str, model_string: str) -> None:
    """Set the active model string for a role and invalidate caches/clients."""
    if role not in MODEL_ROLES:
        raise ValueError(f"Unknown model role: {role!r} (expected one of {MODEL_ROLES})")
    async with session_scope() as session:
        stmt = pg_insert(ActiveModel).values(role=role, model_string=model_string)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ActiveModel.role],
            set_={"model_string": model_string},
        )
        await session.execute(stmt)
    _invalidate_active_models_cache()


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
            select(ChatState.summary).where(ChatState.chat_id == chat_id)
        )


async def get_summary_mood(chat_id: int) -> tuple[Union[str, None], Union[str, None]]:
    """Fetch the persisted ``(summary, mood)`` for a chat, or ``(None, None)``."""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(ChatState.summary, ChatState.mood).where(
                    ChatState.chat_id == chat_id
                )
            )
        ).first()
        return (row[0], row[1]) if row is not None else (None, None)


async def set_summary(chat_id: int, summary: str, mood: str = "default") -> None:
    """Upsert the summary and last-detected mood for a chat."""
    summary = summary or ""  # summary column is NOT NULL; coerce None → ""
    async with session_scope() as session:
        stmt = pg_insert(ChatState).values(chat_id=chat_id, summary=summary, mood=mood)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChatState.chat_id],
            set_={"summary": summary, "mood": mood},
        )
        await session.execute(stmt)


async def delete_summary(chat_id: int) -> None:
    async with session_scope() as session:
        await session.execute(delete(ChatState).where(ChatState.chat_id == chat_id))


async def get_last_processed_message_id(chat_id: int) -> int:
    """Return the memory-pipeline high-water mark for a chat (0 if none yet)."""
    async with session_scope() as session:
        val = await session.scalar(
            select(ChatState.last_processed_message_id).where(ChatState.chat_id == chat_id)
        )
    return val or 0


async def set_last_processed_message_id(chat_id: int, message_id: int) -> None:
    """Advance a chat's memory-pipeline watermark, inserting the row if needed.

    Only ``last_processed_message_id`` is written; summary/mood are left to their
    own upsert (or the row's defaults on first insert)."""
    async with session_scope() as session:
        stmt = pg_insert(ChatState).values(
            chat_id=chat_id, last_processed_message_id=message_id
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChatState.chat_id],
            set_={"last_processed_message_id": message_id},
        )
        await session.execute(stmt)


# --- user profiles ----------------------------------------------------------
# Free-form user facts now live in Graphiti/Neo4j (see app/services/worldview.py
# and app/services/userfacts.py); only the fixed-slot profile stays in Postgres.


async def delete_user_profile(user_id: int) -> None:
    async with session_scope() as session:
        await session.execute(
            update(UserFactsPreferences)
            .where(UserFactsPreferences.user_id == user_id)
            .values(profile=None)
        )


async def get_user_profile(user_id: int) -> dict:
    """Return the structured profile dict for a user, or {} if none stored yet."""
    async with session_scope() as session:
        profile = await session.scalar(
            select(UserFactsPreferences.profile).where(UserFactsPreferences.user_id == user_id)
        )
    return profile or {}


async def get_user_profiles_batch(user_ids: list[int]) -> dict[int, dict]:
    """Return structured profiles for many users in a single query.

    Keyed by user_id; users with an empty/absent profile are omitted.
    """
    if not user_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(UserFactsPreferences.user_id, UserFactsPreferences.profile)
                .where(UserFactsPreferences.user_id.in_(user_ids))
            )
        ).all()
    return {r.user_id: r.profile for r in rows if r.profile}


async def set_user_profile(user_id: int, profile: dict) -> None:
    """Upsert the structured profile blob for a user."""
    async with session_scope() as session:
        stmt = pg_insert(UserFactsPreferences).values(user_id=user_id, profile=profile)
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserFactsPreferences.user_id],
            set_={"profile": profile, "updated_at": func.now()},
        )
        await session.execute(stmt)
