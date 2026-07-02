"""Admin Telethon bot — manage Rachel's system prompt from within Telegram.

Ported from Reference/app/bot.py. Repository calls are now awaited, and the bot
is started/stopped by app.main's lifespan rather than here.
"""

from telethon import TelegramClient, events

from app.config import get_settings
from app.prompts import USER_PROFILE_FIELDS
from app.services.userfacts import add_user_facts, get_user_facts
from app.repository import (
    clear_history,
    delete_summary,
    delete_user_profile,
    get_all_chats,
    get_all_users,
    get_history,
    get_summarizer_system_prompt,
    get_summary,
    get_responder_system_prompt,
    get_traits,
    get_user_profile,
    reset_traits,
    set_summarizer_system_prompt,
    set_responder_system_prompt,
    set_trait_value,
    set_user_profile,
)

settings = get_settings()

bot = TelegramClient("bot", settings.telegram_api_id, settings.telegram_api_hash)

ADMIN = settings.admin_id


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern="\\/get_responder_system_prompt")
)
async def on_get_responder_system_prompt(event):
    prompt = await get_responder_system_prompt()
    chunk_size = 4000
    for i in range(0, len(prompt), chunk_size):
        await event.reply(prompt[i : i + chunk_size])


@bot.on(
    events.NewMessage(
        incoming=True, from_users=[ADMIN], pattern="\\/set_responder_system_prompt\\s.*"
    )
)
async def on_set_responder_system_prompt(event):
    new_system_prompt = event.raw_text.replace("/set_responder_system_prompt ", "")
    await set_responder_system_prompt(new_system_prompt)
    await event.reply("System prompt set!")


@bot.on(
    events.NewMessage(
        incoming=True, from_users=[ADMIN], pattern="\\/get_summarizer_system_prompt"
    )
)
async def on_get_summarizer_system_prompt(event):
    prompt = await get_summarizer_system_prompt()
    chunk_size = 4000
    for i in range(0, len(prompt), chunk_size):
        await event.reply(prompt[i : i + chunk_size])


@bot.on(
    events.NewMessage(
        incoming=True, from_users=[ADMIN], pattern="\\/set_summarizer_system_prompt\\s.*"
    )
)
async def on_set_summarizer_system_prompt(event):
    new_summarizer_system_prompt = event.raw_text.replace("/set_summarizer_system_prompt ", "")
    await set_summarizer_system_prompt(new_summarizer_system_prompt)
    await event.reply("Summarizer system prompt set!")


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/list_user_names$")
)
async def on_list_user_names(event):
    users = await get_all_users()
    if not users:
        await event.reply("No users found.")
        return
    lines = [
        f"{u['telegram_user_id']} | @{u['username'] or '—'} | {u['first_name'] or ''} {u['last_name'] or ''}".strip()
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
    lines = []
    for i in items:
        lines.append(f"[{i['telegram_message_id']}] {i['sender']}: {i['content']}")
        if i.get("reason"):
            lines.append(f"    ↳ reason: {i['reason']}")
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


# Free-form user facts live in the Graphiti knowledge graph (one Neo4j group_id
# per user), so the only fact commands are /get_user_facts (dump every episode
# in the user's partition) and /add_user_facts (ingest a new fact episode, the
# same path as the pipeline's ingest_node). No edit/delete: Graphiti's own
# dedup/temporal conflict resolution supersedes old facts on ingest.


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/get_user_facts(\s+(-?\d+))?$")
)
async def on_get_user_facts(event):
    if event.pattern_match.group(2) is None:
        await event.reply("Usage: /get_user_facts <user_id>")
        return
    user_id = int(event.pattern_match.group(2))
    try:
        facts = await get_user_facts(user_id)
    except Exception as e:
        await event.reply(f"Failed to read facts for user {user_id}: {e}")
        return
    if not facts:
        await event.reply(f"No facts stored for user {user_id}.")
        return
    text = f"Facts for user {user_id}:\n" + "\n".join(f"- {f}" for f in facts)
    # Telegram message limit is 4096 chars
    if len(text) > 4000:
        text = f"(truncated to last 4000 chars)\n{text[-4000:]}"
    await event.reply(text)


@bot.on(
    events.NewMessage(
        incoming=True, from_users=[ADMIN], pattern=r"\/add_user_facts\s+(-?\d+)\s+([\s\S]+)$"
    )
)
async def on_add_user_facts(event):
    user_id = int(event.pattern_match.group(1))
    fact = event.pattern_match.group(2).strip()
    # Ingestion is several LLM round-trips per fact, so acknowledge first.
    await event.reply(f"Ingesting fact for user {user_id}… (this takes a while)")
    try:
        await add_user_facts(user_id, [fact])
    except Exception as e:
        await event.reply(f"Failed to ingest fact for user {user_id}: {e}")
        raise events.StopPropagation
    await event.reply(f"Fact ingested for user {user_id}.")
    raise events.StopPropagation


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/add_user_facts(\s+.*)?$")
)
async def on_add_user_facts_usage(event):
    await event.reply("Usage: /add_user_facts <user_id> <fact text>")


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/get_user_profile(\s+(-?\d+))?$")
)
async def on_get_user_profile(event):
    if event.pattern_match.group(2) is None:
        await event.reply("Usage: /get_user_profile <user_id>")
        return
    user_id = int(event.pattern_match.group(2))
    profile = await get_user_profile(user_id)
    profile_text = "\n".join(
        f"- {label}: {value}"
        for key, label, _guide in USER_PROFILE_FIELDS
        if (value := str(profile.get(key, "")).strip())
    )
    if not profile_text:
        await event.reply(f"No profile for user {user_id}.")
        return
    await event.reply(f"Profile for user {user_id}:\n{profile_text}")


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/delete_user_profile(\s+(-?\d+))?$")
)
async def on_delete_user_profile(event):
    if event.pattern_match.group(2) is None:
        await event.reply("Usage: /delete_user_profile <user_id>")
        return
    user_id = int(event.pattern_match.group(2))
    await delete_user_profile(user_id)
    await event.reply(f"Profile deleted for user {user_id}.")


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/list_traits$")
)
async def on_list_traits(event):
    traits = await get_traits()
    if not traits:
        await event.reply("No traits found.")
        return
    lines = [f"[{t['id']}] {t['name']}: {t['current_value']}" for t in traits]
    await event.reply("\n".join(lines))


@bot.on(
    events.NewMessage(
        incoming=True, from_users=[ADMIN], pattern=r"\/set_trait\s+(\d+)\s+(low|medium|high)$"
    )
)
async def on_set_trait(event):
    trait_id = int(event.pattern_match.group(1))
    value = event.pattern_match.group(2)
    found = await set_trait_value(trait_id, value)
    if not found:
        await event.reply(f"No trait with id {trait_id}.")
    else:
        await event.reply(f"Trait {trait_id} set to {value}.")
    raise events.StopPropagation


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/set_trait(\s+.*)?$")
)
async def on_set_trait_usage(event):
    await event.reply("Usage: /set_trait <id> <low|medium|high>")


@bot.on(
    events.NewMessage(incoming=True, from_users=[ADMIN], pattern=r"\/reset_traits$")
)
async def on_reset_traits(event):
    await reset_traits()
    await event.reply("All traits reset to medium.")
