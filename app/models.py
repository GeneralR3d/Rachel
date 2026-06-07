"""SQLAlchemy ORM models mirroring the original SQLite schema.

Original SQLite tables (see Reference/app/db.py):
    SystemPrompt(SystemPrompt TEXT)
    Summary(chat_id INTEGER PK, summary TEXT)
    History(message_id INTEGER PK AUTOINCREMENT, chat_id INTEGER, sender TEXT, content TEXT)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class SystemPrompt(Base):
    __tablename__ = "system_prompts"

    # Single-row table; surrogate PK added (SQLite version had no explicit PK).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    responder_system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    summarizer_system_prompt: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class Summary(Base):
    __tablename__ = "summary"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    username: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PersonalityTrait(Base):
    __tablename__ = "personality_traits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    medium_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    high_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    current_value: Mapped[str] = mapped_column(Text, nullable=False, default="medium")


class History(Base):
    __tablename__ = "history"

    # Telegram message IDs are unique per chat, so the PK is composite.
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # References users.telegram_user_id — no FK constraint to avoid cascade issues.
    sender_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
