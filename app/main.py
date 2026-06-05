"""FastAPI application entry point.

Runs both Telethon clients (Rachel + admin bot) inside the app lifespan, on the
same event loop as uvicorn, and exposes the admin HTTP API. There is no webhook:
Telethon keeps a persistent connection to Telegram and processes pushed events.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.database import dispose_engine
from app.repository import ensure_system_prompt_seeded, ensure_traits_seeded
from app.routers import admin
from app.telegram.bot import bot
from app.telegram.client import client

logger = logging.getLogger("rachel")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    await ensure_system_prompt_seeded()
    await ensure_traits_seeded()

    logger.info("Starting Telethon clients...")
    # Rachel's user-facing client. Requires anon.session to already exist
    # (uvicorn is non-interactive — run `python -m scripts.login` once first).
    await client.start(bot_token=settings.telegram_bot_token)
    await bot.start(bot_token=settings.telegram_bot_token)
    logger.info("Telethon clients started.")

    try:
        yield
    finally:
        # shutdown
        logger.info("Disconnecting Telethon clients...")
        await client.disconnect()
        await bot.disconnect()
        await dispose_engine()


app = FastAPI(title="Rachel", lifespan=lifespan)
app.include_router(admin.router)
