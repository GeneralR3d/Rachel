"""One-time interactive login to create Rachel's ``anon.session``.

uvicorn is non-interactive, so Telethon's first-run prompt for Rachel's bot
token must be done here, once, before serving:

    uv run python -m scripts.login

When prompted "Please enter your phone number (or bot token):", paste Rachel's
bot token (the bot your friends talk to, created via @BotFather). This is saved
to anon.session and never asked again.
"""

import asyncio

from telethon import TelegramClient

from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    client = TelegramClient(
        "anon", settings.telegram_api_id, settings.telegram_api_hash
    )
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.username or me.id}. Session saved to anon.session.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
