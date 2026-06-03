"""Async data-access layer — a port of the original Reference/app/db.py.

Function names and signatures are kept identical so the Telethon handlers and
HTTP routes change as little as possible. Each function opens its own session
(mirroring the original "connect per call" style) via ``session_scope``.
"""

from typing import Optional, Union

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import session_scope
from app.models import History, Summary, SystemPrompt
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


# --- history -------------------------------------------------------------


async def get_history(chat_id: int, count: int = -1) -> list[dict]:
    """count: number of pieces to get (earliest first), -1 for all."""
    stmt = (
        select(History.sender, History.content, History.message_id)
        .where(History.chat_id == chat_id)
        .order_by(History.message_id.asc())
    )
    if count != -1:
        stmt = stmt.limit(count)

    async with session_scope() as session:
        rows = (await session.execute(stmt)).all()

    return [
        {"sender": r.sender, "content": r.content, "message_id": r.message_id}
        for r in rows
    ]


async def add_history(
    chat_id: int, sender: str, content: str, message_id: int = None
) -> None:
    values = {"chat_id": chat_id, "sender": sender, "content": content}
    if message_id:
        values["message_id"] = message_id

    async with session_scope() as session:
        await session.execute(pg_insert(History).values(**values))


async def add_history_batch(
    chat_ids: list[int],
    senders: list[str],
    contents: list[str],
    message_ids: list[int] = None,
) -> None:
    """Insert multiple history entries. Works for a single message too."""
    if not len(chat_ids) == len(senders) == len(contents):
        raise ValueError(
            "chat_ids, senders, and contents must be lists of the same length"
        )
    if message_ids is not None and len(message_ids) != len(chat_ids):
        raise ValueError("message_ids must have the same length as other lists")

    rows: list[dict] = []
    for i in range(len(chat_ids)):
        row = {
            "chat_id": chat_ids[i],
            "sender": senders[i],
            "content": contents[i],
        }
        if message_ids is not None:
            row["message_id"] = message_ids[i]
        rows.append(row)

    if not rows:
        return

    async with session_scope() as session:
        await session.execute(pg_insert(History), rows)


async def clear_history(chat_id: int) -> None:
    async with session_scope() as session:
        await session.execute(delete(History).where(History.chat_id == chat_id))


async def get_history_min_id(chat_id: int) -> int:
    """Min message_id stored for a chat, used to incrementally update history."""
    async with session_scope() as session:
        out = await session.scalar(
            select(func.min(History.message_id)).where(History.chat_id == chat_id)
        )
    # return 0 if no min id (no messages stored yet)
    return out if out else 0


async def rewrite_history(chat_id: int, parsed_history: list[dict]) -> None:
    async with session_scope() as session:
        await session.execute(delete(History).where(History.chat_id == chat_id))
        for item in parsed_history:
            await session.execute(
                pg_insert(History).values(
                    message_id=item["message_id"],
                    chat_id=chat_id,
                    sender=item["sender"],
                    content=item["content"],
                )
            )


async def get_history_length(chat_id: int) -> int:
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count(History.message_id)).where(History.chat_id == chat_id)
        )
    return count or 0


async def delete_history(chat_id: int, message_ids: list[int]) -> None:
    """For summary purposes — delete specific messages."""
    if not message_ids:
        return
    async with session_scope() as session:
        await session.execute(
            delete(History).where(
                History.chat_id == chat_id, History.message_id.in_(message_ids)
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
