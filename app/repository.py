"""Async data-access layer — a port of the original Reference/app/db.py.

Function names and signatures are kept identical so the Telethon handlers and
HTTP routes change as little as possible. Each function opens its own session
(mirroring the original "connect per call" style) via ``session_scope``.
"""

from typing import Optional, Union

import sqlalchemy as sa
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import session_scope
from app.models import History, PersonalityTrait, Summary, SystemPrompt, User
from app.prompts import DEFAULT_TRAITS, SUMMARIZER_SYSTEM_PROMPT, RESPONDER_SYSTEM_PROMPT

# Cached system prompts (mirrors the original module-level global cache).
_responder_system_prompt: Optional[str] = None
_summarizer_system_prompt: Optional[str] = None


async def ensure_traits_seeded() -> None:
    """Insert default personality traits only if the table is empty."""
    async with session_scope() as session:
        existing = await session.scalar(select(PersonalityTrait.id).limit(1))
        if existing is None:
            for trait in DEFAULT_TRAITS:
                trait_data = {k: v for k, v in trait.items() if k != "default_value"}
                session.add(PersonalityTrait(**trait_data, current_value=trait.get("default_value", "medium")))


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


async def set_trait_value(trait_id: int, value: str) -> bool:
    """Set current_value for a trait. Returns False if the trait does not exist."""
    async with session_scope() as session:
        result = await session.execute(
            update(PersonalityTrait)
            .where(PersonalityTrait.id == trait_id)
            .values(current_value=value)
        )
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


async def get_active_trait_prompts() -> str:
    """Assemble all active trait prompts into a single block for the system prompt."""
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
    return "\n".join(lines)


async def ensure_system_prompts_seeded() -> None:
    """Insert the default system prompt only if the table is empty.

    Replaces db.py's import-time INSERT, which blindly added a new row on every
    startup. Called once during application startup.
    """
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
            current = await session.scalar(select(SystemPrompt.summarizer_system_prompt).limit(1))
            if not current:
                await session.execute(
                    update(SystemPrompt).values(summarizer_system_prompt=SUMMARIZER_SYSTEM_PROMPT)
                )


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
    """count: number of pieces to get (earliest first), -1 for all.

    Returns dicts with ``sender`` set to the user's display name (username,
    first_name, or stringified ID) for backward compatibility with the LLM layer.
    """
    display_name = func.coalesce(
        User.username,
        User.first_name,
        sa.cast(History.sender_user_id, sa.Text),
    ).label("sender")

    stmt = (
        select(History.chat_id, History.sender_user_id, History.content, History.telegram_message_id, display_name)
        .outerjoin(User, History.sender_user_id == User.telegram_user_id)
        .where(History.chat_id == chat_id)
        .order_by(History.telegram_message_id.asc())
    )
    if count != -1:
        stmt = stmt.limit(count)

    async with session_scope() as session:
        rows = (await session.execute(stmt)).all()

    return [
        {
            "sender": r.sender,
            "sender_user_id": r.sender_user_id,
            "content": r.content,
            "telegram_message_id": r.telegram_message_id,
            "chat_id":r.chat_id
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
) -> None:
    """Insert multiple history entries. Works for a single message too."""
    if not len(chat_ids) == len(sender_user_ids) == len(contents) == len(telegram_message_ids):
        raise ValueError(
            "chat_ids, sender_user_ids, contents, and telegram_message_ids must be lists of the same length"
        )

    rows = [
        {
            "chat_id": chat_ids[i],
            "sender_user_id": sender_user_ids[i],
            "content": contents[i],
            "telegram_message_id": telegram_message_ids[i],
        }
        for i in range(len(chat_ids))
    ]

    if not rows:
        return

    async with session_scope() as session:
        await session.execute(pg_insert(History), rows)


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


async def get_history_length(chat_id: int) -> int:
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count(History.telegram_message_id)).where(History.chat_id == chat_id)
        )
    return count or 0


async def delete_history(chat_id: int, telegram_message_ids: list[int]) -> None:
    """For summary purposes — delete specific messages."""
    if not telegram_message_ids:
        return
    async with session_scope() as session:
        await session.execute(
            delete(History).where(
                History.chat_id == chat_id,
                History.telegram_message_id.in_(telegram_message_ids),
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
