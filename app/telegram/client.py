"""Rachel's user-facing Telethon client and event handlers.

Ported from Reference/app/client.py. Behaviour is unchanged; the only edits are:
  - the client is built from app.config settings,
  - all (now async) repository calls are awaited,
  - get_response is awaited (already async),
  - the client is NOT started here — app.main's lifespan owns the lifecycle.
"""

import asyncio
import time
from pprint import pprint
from typing import Dict, List, Optional

from pydantic import BaseModel
from telethon import TelegramClient, events

from app.config import get_settings
from app.repository import (
    add_history_batch,
    clear_history,
    delete_summary,
    get_history,
    get_history_min_id,
    get_summary,
    rewrite_history,
    set_summary,
    upsert_user,
)
from app.services.llm import get_response
from app.services.worldview import update_worldview
from app.telegram.bot import ADMIN
from app.utils import parse_history

settings = get_settings()

client = TelegramClient("anon", settings.telegram_api_id, settings.telegram_api_hash)

# constants
REPLY_DELAY = 5              # seconds to wait after last message before replying
CHAT_BLACKOUT_TIME = 60          # 3 min of inactivity before flushing buffer to DB
N_PAST_MSG_REQUIRED = 20         # messages pre-loaded on first contact and fed to LLM as context
MAX_BUFFER_LEN = 150             # flush to DB immediately if buffer hits this length
TYPING_SPEED = 20                # characters per second

# only used for summarisation
USER_NAME = settings.user_name
BOT_NAME = settings.bot_name


class BufferedMessage(BaseModel):
    telegram_message_id: int
    sender_user_id: int
    sender_name: str
    content: str
    is_persisted: bool = False
    # The responder LLM's one-sentence justification; None for user messages.
    reason: Optional[str] = None

    def to_llm_dict(self) -> Dict[str, str]:
        return {"sender": self.sender_name, "content": self.content}


# state
current_messages_buffer: Dict[int, List[BufferedMessage]] = {}
pending_summaries: Dict[int, str] = {}
wait_tasks: Dict[int, asyncio.Task] = {}
flush_tasks: Dict[int, asyncio.Task] = {}
last_message_time: Dict[int, float] = {}


# helper functions


async def reply(event):
    """
    Feed the most recent N_PAST_MSG_REQUIRED messages from the buffer as context,
    call the LLM, send the reply, and append the bot response back into the buffer.
    Persistence is handled by finalize_conversation().
    """
    chat_id = event.chat_id
    buffer = current_messages_buffer[chat_id]
    me = await client.get_me()

    if not client.is_connected():
        print(f"[{chat_id}] Client disconnected — skipping reply")
        return

    async with client.action(event.chat_id, "typing"):  # pyright: ignore
        if chat_id in pending_summaries:
            current_summary = pending_summaries.get(chat_id)
        else:
            current_summary = await get_summary(chat_id)
            pending_summaries[chat_id] = current_summary
            
        context = [m.to_llm_dict() for m in buffer[-N_PAST_MSG_REQUIRED:]]

        try:
            response, response_reason, new_summary, load_time = await get_response(
                history=context,
                current_summary=current_summary,
                chat_id=chat_id,
            )
        except Exception as e:
            print(f"[{chat_id}] get_response failed ({type(e).__name__}: {e}), retrying once...")
            response, response_reason, new_summary, load_time = await get_response(
                history=context,
                current_summary=current_summary,
                chat_id=chat_id,
            )

        if new_summary is not None:
            pending_summaries[chat_id] = new_summary
            print(f"[{chat_id}] Summary buffered (pending flush): {new_summary}")
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

    if bot_message_id is not None and chat_id in current_messages_buffer:
        current_messages_buffer[chat_id].append(
            BufferedMessage(
                telegram_message_id=bot_message_id,
                sender_user_id=me.id,
                sender_name=BOT_NAME,
                content=response,
                is_persisted=False,
                reason=response_reason,
            )
        )

    if len(current_messages_buffer.get(chat_id, [])) >= MAX_BUFFER_LEN:
        print(f"[{chat_id}] Buffer hit MAX_BUFFER_LEN ({MAX_BUFFER_LEN}), flushing immediately")
        if chat_id in flush_tasks and not flush_tasks[chat_id].done():
            flush_tasks[chat_id].cancel()
        flush_tasks[chat_id] = asyncio.create_task(_flush_chat(chat_id))


async def wait_before_reply(event, delay: int):
    """Wait ``delay`` seconds, then reply (awaited so it completes in-task)."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return  # New message arrived during wait — correct, abort
    # Past the delay: committed to replying. Shield the LLM call from external
    # cancellation so a racing new message doesn't kill an in-flight response.
    try:
        await asyncio.shield(reply(event))
    except asyncio.CancelledError:
        pass  # Outer task was cancelled but reply() continues shielded


async def _flush_chat(chat_id: int) -> None:
    """Persist all unpersisted buffer messages for one chat and clear its entry."""
    to_persist = [m for m in current_messages_buffer.get(chat_id, []) if not m.is_persisted]

    if to_persist:
        await add_history_batch(
            chat_ids=[chat_id] * len(to_persist),
            sender_user_ids=[m.sender_user_id for m in to_persist],
            contents=[m.content for m in to_persist],
            telegram_message_ids=[m.telegram_message_id for m in to_persist],
            reasons=[m.reason for m in to_persist],
        )
        print(f"[{chat_id}] Flushed {len(to_persist)} messages to DB")

    if chat_id in pending_summaries:
        await set_summary(chat_id, pending_summaries.pop(chat_id))
        print(f"[{chat_id}] Flushed summary to DB")

    #TODO: fix this gap. Any messages that arrive between these will be lost.

    current_messages_buffer.pop(chat_id, None)
    last_message_time.pop(chat_id, None)


async def finalize_conversation(chat_id: int, delay: float):
    """After ``delay`` seconds of inactivity, treat the conversation as finished:
    persist unpersisted buffer messages to DB and extract world-view facts.

    This is the "conversation finished" signal, so it also kicks off world-view
    fact extraction for the dialogue that just ended. The conversation is
    snapshotted *before* _flush_chat clears the buffer, and extraction runs as a
    detached task so it never blocks the flush (and never runs on the shutdown
    path, which calls _flush_chat directly).
    """
    await asyncio.sleep(delay)
    conversation = [m.to_llm_dict() for m in current_messages_buffer.get(chat_id, [])]
    await _flush_chat(chat_id)
    if conversation:
        asyncio.create_task(update_worldview(conversation))
        print("Worldview pipleline called")


async def flush_all_buffers() -> None:
    """Cancel all pending tasks and flush every chat's buffer to DB.

    Called on graceful shutdown (SIGTERM / lifespan teardown) so in-memory
    messages are not lost when the process exits.
    """
    for task in list(wait_tasks.values()) + list(flush_tasks.values()):
        task.cancel()
    wait_tasks.clear()
    flush_tasks.clear()

    chat_ids = list(current_messages_buffer.keys())
    for chat_id in chat_ids:
        try:
            await _flush_chat(chat_id)
        except Exception as e:
            print(f"[{chat_id}] Error flushing buffer on shutdown: {e}")



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

    for state in (current_messages_buffer, pending_summaries, last_message_time, wait_tasks, flush_tasks):
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
    if event.raw_text.startswith("/") and event.sender_id == ADMIN:
        return  # slash commands are handled by dedicated handlers; don't feed them to the LLM
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
    flush_tasks[chat_id] = asyncio.create_task(finalize_conversation(chat_id, CHAT_BLACKOUT_TIME))


@client.on(events.ChatAction)
async def say_hi_added(event):
    """Send a greeting when the bot is added to a group."""
    if event.user_added or event.user_joined:
        me = await client.get_me()
        if event.user_id == me.id:
            print(f"Bot added to group: {event.chat_id}")
            await event.respond("Hi Im Rachel! Nice to meet you!")
