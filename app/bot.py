from telethon import TelegramClient, events

import os
from dotenv import load_dotenv

from app.db import get_system_prompt, set_system_prompt

load_dotenv()

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
bot = TelegramClient("bot", api_id, api_hash)

ADMIN = int(os.environ["ADMIN_ID"])


@bot.on(events.NewMessage(incoming=True, from_users=[ADMIN], pattern="\\/get_system_prompt"))
async def on_get_system_prompt(event):
    await event.reply(get_system_prompt())


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern="\\/set_system_prompt\\s.*")
)
async def on_set_system_prompt(event):
    new_system_prompt = event.raw_text.replace("/set_system_prompt ", "")
    set_system_prompt(new_system_prompt)
    await event.reply(("System prompt set!"))
