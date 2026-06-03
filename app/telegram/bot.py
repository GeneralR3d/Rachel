"""Admin Telethon bot — manage Rachel's system prompt from within Telegram.

Ported from Reference/app/bot.py. Repository calls are now awaited, and the bot
is started/stopped by app.main's lifespan rather than here.
"""

from telethon import TelegramClient, events

from app.config import get_settings
from app.repository import get_system_prompt, set_system_prompt

settings = get_settings()

bot = TelegramClient("bot", settings.telegram_api_id, settings.telegram_api_hash)

ADMIN = settings.admin_id


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern="\\/get_system_prompt")
)
async def on_get_system_prompt(event):
    await event.reply(await get_system_prompt())


@bot.on(
    events.NewMessage(
        incoming=True, from_users=[ADMIN], pattern="\\/set_system_prompt\\s.*"
    )
)
async def on_set_system_prompt(event):
    new_system_prompt = event.raw_text.replace("/set_system_prompt ", "")
    await set_system_prompt(new_system_prompt)
    await event.reply("System prompt set!")
