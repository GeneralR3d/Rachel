"""Admin Telethon bot — manage Rachel's system prompt from within Telegram.

Ported from Reference/app/bot.py. Repository calls are now awaited, and the bot
is started/stopped by app.main's lifespan rather than here.
"""

from telethon import TelegramClient, events

from app.config import get_settings
from app.repository import (
    clear_history,
    delete_summary,
    get_all_chats,
    get_all_users,
    get_history,
    get_summary,
    get_system_prompt,
    set_system_prompt,
)

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


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/list_user_names$")
)
async def on_list_user_names(event):
    users = await get_all_users()
    if not users:
        await event.reply("No users found.")
        return
    lines = [
        f"@{u['username'] or '—'} | {u['first_name'] or ''} {u['last_name'] or ''}".strip()
        for u in users
    ]
    await event.reply("\n".join(lines))


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/list_chats$")
)
async def on_list_chats(event):
    chats = await get_all_chats()
    if not chats:
        await event.reply("No chats in history.")
        return
    lines = [f"{c['chat_id']} — {c['message_count']} messages" for c in chats]
    await event.reply("\n".join(lines))


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/get_history(\s+(-?\d+))?$")
)
async def on_get_history(event):
    if event.pattern_match.group(2) is None:
        await event.reply("Usage: /get_history <chat_id>")
        return
    chat_id = int(event.pattern_match.group(2))
    items = await get_history(chat_id)
    if not items:
        await event.reply(f"No history for chat {chat_id}.")
        return
    lines = [f"[{i['telegram_message_id']}] {i['sender']}: {i['content']}" for i in items]
    text = "\n".join(lines)
    # Telegram message limit is 4096 chars
    if len(text) > 4000:
        text = text[-4000:]
        text = f"(truncated to last 4000 chars)\n{text}"
    await event.reply(text)


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/clear_history(\s+(-?\d+))?$")
)
async def on_clear_history(event):
    if event.pattern_match.group(2) is None:
        await event.reply("Usage: /clear_history <chat_id>")
        return
    chat_id = int(event.pattern_match.group(2))
    await clear_history(chat_id)
    await event.reply(f"History cleared for chat {chat_id}.")


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/get_summary(\s+(-?\d+))?$")
)
async def on_get_summary(event):
    if event.pattern_match.group(2) is None:
        await event.reply("Usage: /get_summary <chat_id>")
        return
    chat_id = int(event.pattern_match.group(2))
    summary = await get_summary(chat_id)
    if summary is None:
        await event.reply(f"No summary for chat {chat_id}.")
    else:
        await event.reply(f"Summary for {chat_id}:\n{summary}")


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/delete_summary(\s+(-?\d+))?$")
)
async def on_delete_summary(event):
    if event.pattern_match.group(2) is None:
        await event.reply("Usage: /delete_summary <chat_id>")
        return
    chat_id = int(event.pattern_match.group(2))
    await delete_summary(chat_id)
    await event.reply(f"Summary deleted for chat {chat_id}.")
