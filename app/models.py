"""SQLAlchemy ORM models mirroring the original SQLite schema.

Original SQLite tables (see Reference/app/db.py):
    SystemPrompt(SystemPrompt TEXT)
    Summary(chat_id INTEGER PK, summary TEXT)
    History(message_id INTEGER PK AUTOINCREMENT, chat_id INTEGER, sender TEXT, content TEXT)
"""

from sqlalchemy import BigInteger, Identity, Index, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SystemPrompt(Base):
    __tablename__ = "system_prompt"

    # Single-row table; surrogate PK added (SQLite version had no explicit PK).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)


class Summary(Base):
    __tablename__ = "summary"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


class History(Base):
    __tablename__ = "history"

    # Identity column: auto-generated when not supplied, but explicit inserts
    # (used by add_history / rewrite_history) are still allowed via OVERRIDING.
    message_id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (Index("ix_history_chat_id", "chat_id"),)
