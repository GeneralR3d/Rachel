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
from app.models import History, Summary, SystemPrompt, User
from app.prompts import SYSTEM_PROMPT

# Cached system prompt (mirrors the original module-level global cache).
_system_prompt: Optional[str] = None


async def ensure_system_prompt_seeded() -> None:
    """Insert the default system prompt only if the table is empty.

    Replaces db.py's import-time INSERT, which blindly added a new row on every
    startup. Called once during application startup.
    """
    async with session_scope() as session:
        existing = await session.scalar(select(SystemPrompt.id).limit(1))
        if existing is None:
            session.add(SystemPrompt(system_prompt=SYSTEM_PROMPT))


# --- system prompt -------------------------------------------------------


async def get_system_prompt() -> str:
    global _system_prompt
    if _system_prompt:
        return _system_prompt

    async with session_scope() as session:
        _system_prompt = await session.scalar(
            select(SystemPrompt.system_prompt).limit(1)
        )
    return _system_prompt


async def set_system_prompt(new_system_prompt: str) -> None:
    global _system_prompt
    _system_prompt = new_system_prompt

    async with session_scope() as session:
        await session.execute(
            update(SystemPrompt).values(system_prompt=new_system_prompt)
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
