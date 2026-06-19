"""FastAPI application entry point.

Runs both Telethon clients (Rachel + admin bot) inside the app lifespan, on the
same event loop as uvicorn, and exposes the admin HTTP API. There is no webhook:
Telethon keeps a persistent connection to Telegram and processes pushed events.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.config import get_settings
from app.database import dispose_engine
from app.repository import ensure_schedule_seeded, ensure_system_prompts_seeded, ensure_traits_seeded, upsert_user
from app.routers import admin
from app.telegram.bot import bot
from app.telegram.client import client, flush_all_buffers

logger = logging.getLogger("rachel")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    await ensure_system_prompts_seeded()
    await ensure_traits_seeded()
    await ensure_schedule_seeded()

    logger.info("Starting Telethon clients...")
    # Rachel's user-facing client. Requires anon.session to already exist
    # (uvicorn is non-interactive — run `python -m scripts.login` once first).
    await client.start(bot_token=settings.telegram_bot_token)
    await bot.start(bot_token=settings.telegram_bot_token)
    logger.info("Telethon clients started.")

    me = await client.get_me()
    await upsert_user(
        telegram_user_id=me.id,
        first_name=getattr(me, "first_name", None),
        last_name=getattr(me, "last_name", None),
        username=getattr(me, "username", None),
    )

    try:
        yield
    finally:
        # shutdown
        logger.info("Flushing message buffers...")
        await flush_all_buffers()
        logger.info("Disconnecting Telethon clients...")
        await client.disconnect()
        await bot.disconnect()
        await dispose_engine()


app = FastAPI(title="Rachel", lifespan=lifespan)
app.include_router(admin.router)

_DASHBOARD = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(_DASHBOARD)
