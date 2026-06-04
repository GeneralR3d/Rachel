"""SQLAlchemy ORM models mirroring the original SQLite schema.

Original SQLite tables (see Reference/app/db.py):
    SystemPrompt(SystemPrompt TEXT)
    Summary(chat_id INTEGER PK, summary TEXT)
    History(message_id INTEGER PK AUTOINCREMENT, chat_id INTEGER, sender TEXT, content TEXT)
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


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

    # Telegram message IDs are unique per chat, so the PK is composite.
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
