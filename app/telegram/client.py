"""Rachel's user-facing Telethon client and event handlers.

Ported from Reference/app/client.py. Behaviour is unchanged; the only edits are:
  - the client is built from app.config settings,
  - all (now async) repository calls are awaited,
  - get_response / summarise_text are awaited (already async),
  - the client is NOT started here — app.main's lifespan owns the lifecycle.
"""

import asyncio
import random
from pprint import pprint
from typing import Dict, List

from telethon import TelegramClient, events

from app.config import get_settings
from app.repository import (
    add_history,
    add_history_batch,
    clear_history,
    delete_history,
    delete_summary,
    get_history,
    get_history_length,
    get_history_min_id,
    get_summary,
    rewrite_history,
    set_summary,
)
from app.services.gemini import get_response, summarise_text
from app.utils import parse_history

settings = get_settings()

client = TelegramClient("anon", settings.telegram_api_id, settings.telegram_api_hash)

# constants
MAX_OFFLINE_TIME = 10  # seconds
TYPING_SPEED = 20  # characters per second
HISTORY_LENGTH_THRESHOLD = 500  # when to initiate summary
HISTORY_LENGTH_TO_SUMMARISE = 20  # number of histories to take out and summarise
INPUT_BUFFER_SIZE = 15

# only used for summarisation
USER_NAME = settings.user_name
BOT_NAME = settings.bot_name

# state
current_message_ids: dict = {}
current_message_parts: dict = {}
wait_tasks: dict = {}
cancel_tasks: dict = {}
client_typing_statuses: dict = {}
summarising_statuses: dict = {}


# helper functions


async def reply(event):
    """
    retrieve and clear message parts
    initiate api call
    send reply on tg
    check if history is too long, if so, initiate summary task
    """
    chat_id = event.chat_id

    global current_message_parts
    if chat_id not in current_message_parts or len(current_message_parts[chat_id]) == 0:
        print(f"[{chat_id}] No messages to reply to, exiting")
        return

    current_messages: List[Dict[str, str]] = [
        {
            "sender": part["sender_name"],
            "content": part["content"],
            "message_id": part["message_id"],
        }
        for part in current_message_parts[chat_id]
    ]
    current_message_parts[chat_id].clear()

    async with client.action(event.chat_id, "typing"):  # pyright: ignore
        # a few "human-like" message-response pairs to set the tone of the
        # conversation are appended to the start of the history
        history_and_template: List[Dict[str, str]] = [
            # *CONVERSATION_TONE_TEMPLATES["default"],
            *(await get_history(chat_id)),
        ]
        current_summary = await get_summary(chat_id)

        response, load_time = await get_response(
            current_messages, history_and_template, current_summary
        )  # pyright: ignore

        await add_history_batch(
            message_ids=None,
            chat_ids=[chat_id] * len(current_messages),
            senders=[msg["sender"] for msg in current_messages],
            contents=[msg["content"] for msg in current_messages],
        )
        await add_history(
            chat_id=chat_id, sender=BOT_NAME, content=response, message_id=None
        )

        print(f"[{chat_id}] Current summary: {current_summary}")
        print(f"[{chat_id}] Current history (without prompts) is: ")
        pprint(history_and_template)

    # check if summarising is necessary, if so initiate it
    history_len = await get_history_length(chat_id)
    print(f"[{chat_id}] History length is {history_len} ")
    if history_len > HISTORY_LENGTH_THRESHOLD:
        print(f"[{chat_id}] History length is too long, initiating summarisation")
        if not (chat_id in summarising_statuses and summarising_statuses[chat_id]):
            asyncio.create_task(summarise(chat_id))

    for i, raw_text in enumerate(response.split("\n\n")):
        if raw_text == "":  # speechless
            continue

        current_ai_msg_text = raw_text
        wait = len(current_ai_msg_text) / TYPING_SPEED
        if i != 0:  # dont need to wait before sending first message
            async with client.action(event.chat_id, "typing"):  # pyright: ignore
                await asyncio.sleep(wait)

        await event.respond(current_ai_msg_text)


async def wait_before_reply(event, delay: int):
    """Wait ``delay`` seconds, then reply (awaited so it completes in-task)."""
    await asyncio.sleep(delay)
    await reply(event)


async def stop_typing(chat_id):
    """Background task to set the tracked user typing status back to False."""
    await asyncio.sleep(6)  # typing status lasts for 6s, cancel after 6s

    global client_typing_statuses
    client_typing_statuses[chat_id] = False

    print(f"[{chat_id}] typing=False (timeout)")


async def summarise(chat_id: int):
    """
    grab first X pieces of history
    summarise using model A
    get previous summary if available
    combine summaries if available with model B
    update summary
    delete initially grabbed history (only at this stage in case convo continues)

    NOTE (carried over from the reference): the items returned by get_history use
    keys sender/content/message_id, while this loop reads id/input/response. This
    pre-existing mismatch is preserved verbatim — fix separately if desired.
    """
    print(f"[{chat_id}] Summary initiated")
    summarising_statuses[chat_id] = True

    try:
        to_summarise = await get_history(chat_id, HISTORY_LENGTH_TO_SUMMARISE)
        ids = []
        temp = []
        for item in to_summarise:
            ids.append(item["id"])
            for msg in item["input"].split("\n"):
                temp.append(f"{USER_NAME}: {msg}")
            for msg in item["response"].split("\n"):
                temp.append(f"{BOT_NAME}: {msg}")

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


@client.on(events.UserUpdate())
async def user_update(event):
    """Track the user's typing status; reset it after 6s of no typing events."""
    print(f"[{event.chat_id}] user update received")
    if not event.typing:
        return

    global client_typing_statuses
    client_typing_statuses[event.chat_id] = True

    print(f"[{event.chat_id}] typing=True (event handler)")

    global cancel_tasks
    if event.chat_id in cancel_tasks and cancel_tasks[event.chat_id]:
        cancel_tasks[event.chat_id].cancel()
    cancel_tasks[event.chat_id] = asyncio.create_task(stop_typing(event.chat_id))


@client.on(events.NewMessage(pattern="\\/clear_history"))
async def on_clear_history(event):
    print(f"[{event.chat_id}] clear history command received")

    if event.is_group:
        await event.respond("I can't clear history in groups yet.")
        return  # ignore group messages

    global wait_tasks  # cancel any pending messages
    if event.chat_id in wait_tasks and wait_tasks[event.chat_id]:
        wait_tasks[event.chat_id].cancel()

    await clear_history(event.chat_id)
    await delete_summary(event.chat_id)

    global current_message_ids, current_message_parts
    if event.chat_id in current_message_ids:
        del current_message_ids[event.chat_id]
    if event.chat_id in current_message_parts:
        del current_message_parts[event.chat_id]

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
        if message.from_id is None or message.from_id.user_id != my_id:
            chat_log.insert(0, ("user", message.text, message.id))
        else:
            chat_log.insert(0, ("bot", message.text, message.id))

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
    """
    handle new tg message
    set client_typing status
    cancel current reply if have
    """
    print(f"[{event.chat_id}] new message received")
    chat_id = event.chat_id

    # message received, cancel typing task and immediately set typing False
    global cancel_tasks
    if chat_id in cancel_tasks and cancel_tasks[chat_id]:
        cancel_tasks[chat_id].cancel()

    global client_typing_statuses
    client_typing_statuses[chat_id] = False
    print(f"[{chat_id}] typing=False (message received)")

    global current_message_parts
    if chat_id not in current_message_parts:
        current_message_parts[chat_id] = []

    sender = await event.get_sender()
    event_username = sender.username if sender and sender.username else "Unknown"
    current_message_parts[chat_id].append(
        {
            "message_id": event.message.id,
            "sender_name": event_username,
            "content": event.raw_text,
        }
    )
    print(
        f"[{chat_id}] current_message_parts length: "
        f"{len(current_message_parts[chat_id])}"
    )

    global wait_tasks
    if chat_id not in wait_tasks or wait_tasks[chat_id].done():
        # No wait task (or the existing one is done): start one with a random delay
        delay = random.randint(1, MAX_OFFLINE_TIME)
        wait_tasks[chat_id] = asyncio.create_task(wait_before_reply(event, delay))

    # Buffer threshold reached: reply immediately
    if len(current_message_parts[chat_id]) >= INPUT_BUFFER_SIZE:
        wait_tasks[chat_id].cancel()
        wait_tasks[chat_id] = asyncio.create_task(wait_before_reply(event, 0))


@client.on(events.ChatAction)
async def say_hi_added(event):
    """Send a greeting when the bot is added to a group."""
    if event.user_added or event.user_joined:
        me = await client.get_me()
        if event.user_id == me.id:
            print(f"Bot added to group: {event.chat_id}")
            await event.respond("Hi Im Rachel! Nice to meet you!")
