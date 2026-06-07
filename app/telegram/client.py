"""Rachel's user-facing Telethon client and event handlers.

Ported from Reference/app/client.py. Behaviour is unchanged; the only edits are:
  - the client is built from app.config settings,
  - all (now async) repository calls are awaited,
  - get_response / summarise_text are awaited (already async),
  - the client is NOT started here — app.main's lifespan owns the lifecycle.
"""

import asyncio
import time
from pprint import pprint
from typing import Dict, List

from pydantic import BaseModel
from telethon import TelegramClient, events

from app.config import get_settings
from app.repository import (
    add_history_batch,
    clear_history,
    delete_history,
    delete_summary,
    get_history,
    get_history_min_id,
    get_summary,
    rewrite_history,
    set_summary,
    upsert_user,
)
from app.services.gemini import get_response, summarise_text
from app.utils import parse_history

settings = get_settings()

client = TelegramClient("anon", settings.telegram_api_id, settings.telegram_api_hash)

# constants
REPLY_DELAY = 3                  # seconds to wait after last message before replying
CHAT_BLACKOUT_TIME = 60          # seconds of inactivity before flushing buffer to DB
N_PAST_MSG_REQUIRED = 20         # messages pre-loaded on first contact and fed to LLM as context
TYPING_SPEED = 20                # characters per second
HISTORY_LENGTH_THRESHOLD = 500   # when to initiate summary
HISTORY_LENGTH_TO_SUMMARISE = 20 # number of histories to take out and summarise

# only used for summarisation
USER_NAME = settings.user_name
BOT_NAME = settings.bot_name


class BufferedMessage(BaseModel):
    telegram_message_id: int
    sender_user_id: int
    sender_name: str
    content: str
    is_persisted: bool = False

    def to_llm_dict(self) -> Dict[str, str]:
        return {"sender": self.sender_name, "content": self.content}


# state
current_messages_buffer: Dict[int, List[BufferedMessage]] = {}
wait_tasks: Dict[int, asyncio.Task] = {}
flush_tasks: Dict[int, asyncio.Task] = {}
last_message_time: Dict[int, float] = {}
summarising_statuses: Dict[int, bool] = {}


# helper functions


async def reply(event):
    """
    Feed the most recent N_PAST_MSG_REQUIRED messages from the buffer as context,
    call the LLM, send the reply, and append the bot response back into the buffer.
    Persistence is handled by flush_buffer().
    """
    chat_id = event.chat_id
    buffer = current_messages_buffer[chat_id]
    me = await client.get_me()

    async with client.action(event.chat_id, "typing"):  # pyright: ignore
        current_summary = await get_summary(chat_id)
        context = [m.to_llm_dict() for m in buffer[-N_PAST_MSG_REQUIRED:]]

        response, load_time = await get_response(
            current_messages=[],
            history=context,
            current_summary=current_summary,
        )

        print(f"[{chat_id}] Current summary: {current_summary}")
        print(f"[{chat_id}] Context ({len(context)} messages):")
        pprint(context)

    bot_message_id = None
    for i, raw_text in enumerate(response.split("\n\n")):
        if raw_text == "":
            continue
        wait = len(raw_text) / TYPING_SPEED
        if i != 0: #dont need to wait before sending very first msg
            async with client.action(event.chat_id, "typing"):  # pyright: ignore
                await asyncio.sleep(wait)
        sent = await event.respond(raw_text)
        if bot_message_id is None:
            bot_message_id = sent.id

    if bot_message_id is not None:
        current_messages_buffer[chat_id].append(
            BufferedMessage(
                telegram_message_id=bot_message_id,
                sender_user_id=me.id,
                sender_name=BOT_NAME,
                content=response,
                is_persisted=False,
            )
        )


async def wait_before_reply(event, delay: int):
    """Wait ``delay`` seconds, then reply (awaited so it completes in-task)."""
    await asyncio.sleep(delay)
    await reply(event)


async def flush_buffer(chat_id: int, delay: float):
    """After ``delay`` seconds of inactivity, persist unpersisted buffer messages to DB."""
    await asyncio.sleep(delay)

    if chat_id not in current_messages_buffer:
        return

    to_persist = [m for m in current_messages_buffer[chat_id] if not m.is_persisted]

    if to_persist:
        await add_history_batch(
            chat_ids=[chat_id] * len(to_persist),
            sender_user_ids=[m.sender_user_id for m in to_persist],
            contents=[m.content for m in to_persist],
            telegram_message_ids=[m.telegram_message_id for m in to_persist],
        )
        print(f"[{chat_id}] Flushed {len(to_persist)} messages to DB")

    del current_messages_buffer[chat_id]
    last_message_time.pop(chat_id, None)


async def summarise(chat_id: int):
    """
    grab first X pieces of history
    summarise using model A
    get previous summary if available
    combine summaries if available with model B
    update summary
    delete initially grabbed history (only at this stage in case convo continues)
    """
    print(f"[{chat_id}] Summary initiated")
    summarising_statuses[chat_id] = True

    try:
        to_summarise = await get_history(chat_id, HISTORY_LENGTH_TO_SUMMARISE)
        ids = []
        temp = []
        for item in to_summarise:
            ids.append(item["telegram_message_id"])
            for msg in item["content"].split("\n"):
                temp.append(f"{item['sender']}: {msg}")

        to_summarise_text = "\n".join(temp)
        new_summary = await summarise_text(to_summarise_text)

        current_summary = await get_summary(chat_id)
        if current_summary is not None:
            new_summary = await summarise_text(f"{current_summary}\n{new_summary}")

        await set_summary(chat_id, new_summary)
        await delete_history(chat_id, ids)

        print(f"[{chat_id}] new summary generated: {new_summary}")

    except Exception as e:
        raise e
    finally:
        summarising_statuses[chat_id] = False


# event handlers


@client.on(events.NewMessage(pattern="\\/clear_history"))
async def on_clear_history(event):
    print(f"[{event.chat_id}] clear history command received")

    if event.is_group:
        await event.respond("I can't clear history in groups yet.")
        return  # ignore group messages

    chat_id = event.chat_id

    if chat_id in wait_tasks and wait_tasks[chat_id]:
        wait_tasks[chat_id].cancel()
    if chat_id in flush_tasks and flush_tasks[chat_id]:
        flush_tasks[chat_id].cancel()

    await clear_history(chat_id)
    await delete_summary(chat_id)

    for state in (current_messages_buffer, last_message_time, wait_tasks, flush_tasks):
        state.pop(chat_id, None)

    await event.respond("History cleared.")
    raise events.StopPropagation


@client.on(events.NewMessage(pattern="\\/update_history"))
async def on_update_history(event):
    print(f"[{event.chat_id}] update history command received")

    if event.is_group:
        await event.respond("I can't clear history in groups yet.")
        return  # ignore group messages

    my_id = (await client.get_me(input_peer=True)).user_id
    chat_id = event.chat_id
    min_id = await get_history_min_id(chat_id)

    chat_log = []
    async for message in client.iter_messages(
        chat_id, min_id=min_id - 1, max_id=event.message.id
    ):  # ignore command message
        sender_id = message.from_id.user_id if message.from_id else 0
        chat_log.insert(0, (sender_id, message.text, message.id))

    parsed_history = parse_history(chat_log)
    print(f"{my_id=}")
    print(f"{min_id=}")
    pprint(chat_log)
    pprint(parsed_history)
    await rewrite_history(chat_id, parsed_history)

    # delete command message
    await client.delete_messages(chat_id, [event.message.id])

    raise events.StopPropagation


@client.on(events.NewMessage(incoming=True))
async def new_message(event):
    """Handle new incoming message: populate buffer cache if needed, then schedule a reply."""
    chat_id = event.chat_id
    print(f"[{chat_id}] new message received")

    # On first contact for this chat, pre-load recent history into the buffer
    if chat_id not in current_messages_buffer:
        current_messages_buffer[chat_id] = []
        past = await get_history(chat_id, N_PAST_MSG_REQUIRED)
        for h in past:
            current_messages_buffer[chat_id].append(
                BufferedMessage(
                    telegram_message_id=h["telegram_message_id"],
                    sender_user_id=h["sender_user_id"],
                    sender_name=h["sender"],
                    content=h["content"],
                    is_persisted=True,
                )
            )

    sender = await event.get_sender()
    event_username = sender.username if sender and sender.username else "Unknown"

    if sender:
        await upsert_user(
            telegram_user_id=sender.id,
            first_name=getattr(sender, "first_name", None),
            last_name=getattr(sender, "last_name", None),
            username=getattr(sender, "username", None),
        )

    current_messages_buffer[chat_id].append(
        BufferedMessage(
            telegram_message_id=event.message.id,
            sender_user_id=sender.id if sender else 0,
            sender_name=event_username,
            content=event.raw_text,
            is_persisted=False,
        )
    )
    print(f"[{chat_id}] buffer length: {len(current_messages_buffer[chat_id])}")

    last_message_time[chat_id] = time.time()

    # Always reset the reply timer: respond 3 s after the last message
    if chat_id in wait_tasks and not wait_tasks[chat_id].done():
        wait_tasks[chat_id].cancel()
    wait_tasks[chat_id] = asyncio.create_task(wait_before_reply(event, REPLY_DELAY))

    # Always reset the flush timer: persist buffer 60 s after the last message
    if chat_id in flush_tasks and not flush_tasks[chat_id].done():
        flush_tasks[chat_id].cancel()
    flush_tasks[chat_id] = asyncio.create_task(flush_buffer(chat_id, CHAT_BLACKOUT_TIME))


@client.on(events.ChatAction)
async def say_hi_added(event):
    """Send a greeting when the bot is added to a group."""
    if event.user_added or event.user_joined:
        me = await client.get_me()
        if event.user_id == me.id:
            print(f"Bot added to group: {event.chat_id}")
            await event.respond("Hi Im Rachel! Nice to meet you!")
