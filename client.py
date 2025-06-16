import asyncio
from pprint import pprint
import random
from typing import Dict, List
from telethon import TelegramClient, events, functions
from prompts import CONVERSATION_TONE_TEMPLATES
from api import get_response, summarise_text
from utils import parse_history
from db import (
    get_history,
    add_history,
    add_history_batch,
    clear_history,
    get_history_min_id,
    rewrite_history,
    get_history_length,
    delete_history,
    get_summary,
    set_summary,
    delete_summary,
)

import os
from dotenv import load_dotenv

load_dotenv()

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
client = TelegramClient("anon", api_id, api_hash)

# constants
MAX_OFFLINE_TIME = 30  # seconds
TYPING_SPEED = 20  # characters per second
HISTORY_LENGTH_THRESHOLD = 200  # when to initiate summary
HISTORY_LENGTH_TO_SUMMARISE = 20  # number of histories to take out and summarise
INPUT_BUFFER_SIZE = 15

# only used for summarisation, 
USER_NAME = os.environ.get("USER_NAME")  # user and bot name
BOT_NAME = os.environ.get("BOT_NAME", "Rachel")  # mostly for summary use
ADMIN_ID = int(
    os.environ.get("ADMIN_ID", 0)
)  # whitelist: bot is only accessible to admin (temporarily)

# state
current_message_ids = {}
current_message_parts = {}
wait_tasks = {}
cancel_tasks = {}
client_typing_statuses = {}
summarising_statuses = {}


# helper functions


async def reply(event):
    """
    retrieve and clear message parts
    initiate api call
    send reply on tg
    check if history is too long, if so, initiate summary task
    """
    chat_id = event.chat_id

    # global current_message_ids, current_message_parts
    # current_message_id = current_message_ids[chat_id]
    global current_message_parts
    if chat_id not in current_message_parts or len(current_message_parts[chat_id]) == 0:
        print(f"[{chat_id}] No messages to reply to, exiting")
        return
    
    current_messages : List[Dict[str, str]] = [{"sender": part["sender_name"], "content": part["content"], "message_id": part["message_id"]} for part in current_message_parts[chat_id]]
    # del current_message_ids[chat_id]
    current_message_parts[chat_id].clear()



    async with client.action(event.chat_id, "typing"):  # pyright: ignore

        
        # a few "human-like" message-response pairs to set the tone of the conversation are appended to the start of the history
        history_and_template: List[Dict[str,str]] = [
            #*CONVERSATION_TONE_TEMPLATES["default"],
            *get_history(chat_id),
        ]
        current_summary = get_summary(chat_id)

        response, load_time = await get_response(
            current_messages, history_and_template, current_summary
        )  # pyright: ignore


        add_history_batch(message_ids= [msg["message_id"] for msg in current_messages],
                          chat_ids=[chat_id] * len(current_messages),
                          senders=[msg["sender"] for msg in current_messages],
                          contents=[msg["content"] for msg in current_messages])
        add_history(chat_id=chat_id, sender= BOT_NAME, content=response, message_id=None)
                    
        print(f"[{chat_id}] Current summary: {current_summary}")
        print(f"[{chat_id}] Current history (without prompts) is: ")
        pprint(history_and_template)

    # check if summarising is necessary, if so initiate it
    # idea is, all messages are stored in db, but only the last X messages are needed to keep track of the conversation
    # if the history is too long, summarise it to keep the context of the conversation
    history_len = get_history_length(chat_id)
    print(f"[{chat_id}] History length is {history_len} ")
    if history_len > HISTORY_LENGTH_THRESHOLD:
        # only initiate it if its not already running
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
    """
    This function waits for a delay, checks if the client is still "typing" in a chat, and if so, waits again (recursively). Once the client is not typing, it schedules a reply.

    1. await asyncio.sleep(DELAY) — Waits for a specified delay.
    2. Checks if the client is still typing in the chat.
    3. If typing, it recursively calls itself to wait again.
    4. If not typing, it schedules the reply(event) coroutine as a background task.

    Recursive Await:
    The function uses recursion to wait repeatedly. In Python, deep recursion can cause a stack overflow, but with await, the stack does not grow with each call, so this is safe in an async context.
    
    We cannot create another task from here, because this task will be considered done 
    even when the coroutine reply(event) is still running.
    Instead, we just await reply(event) to ensure it completes before this function returns.
    """
    await asyncio.sleep(delay)
    # if client_typing_statuses.get(event.chat_id, False):
    #     await wait_before_reply(event)
    # else:
    #     await reply(event)  # Wait for reply(event) to complete
    await reply(event)  # Wait for reply(event) to complete

async def stop_typing(chat_id):
    """
    bg task to set typing status back to false
    This does not set the bot's typing status. It is only for keeping track of the user's typing status.
    For its own record.
    """

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
    delete initially grabbed history (only at this stage in case convo continues during summarisation)

    note: there is possibility of generating 2 summaries simultaneously if
    threshold is low but shouldnt happen reasonably
    """
    # dont really have to summarize twice, can optimize abit. Only summarize once, with the new 20 messages from history, and the old summary.
    print(f"[{chat_id}] Summary initiated")
    summarising_statuses[chat_id] = True

    try:
        to_summarise = get_history(chat_id, HISTORY_LENGTH_TO_SUMMARISE)
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

        # if there is already a previous summary, append it with the new one and store it back to the database  
        current_summary = get_summary(chat_id)
        if current_summary != None:
            new_summary = await summarise_text(f"{current_summary}\n{new_summary}")

        # if there is no previous summary, set the new summary as the current one
        set_summary(chat_id, new_summary)
        delete_history(chat_id, ids)

        print(f"[{chat_id}] new summary generated: {new_summary}")

    except Exception as e:
        raise e
    finally:
        summarising_statuses[chat_id] = False


# event handlers


@client.on(events.UserUpdate())
async def user_update(event):
    """
    set typing to true when tg says so
    IF a new typing event is detected before the 6-second timeout expires, the existing stop_typing task is canceled, and a new one is started. This ensures that the typing status remains True as long as typing events are continuously received.
    Not for setting the bot's typing status, but for keeping track of the user's typing status.
    Only when the user stops typing for 6 seconds, then the typing status is set to False. 
    Then the bot starts to reply to the user.
    """
    # only check for typing events
    print(f"[{event.chat_id}] user update received")
    if not event.typing:
        return

    global client_typing_statuses
    client_typing_statuses[event.chat_id] = True

    print(f"[{event.chat_id}] typing=True (event handler)")

    # restart 6s countdown before setting typing to false
    global cancel_tasks
    if event.chat_id in cancel_tasks and cancel_tasks[event.chat_id]:
        cancel_tasks[event.chat_id].cancel()
    cancel_tasks[event.chat_id] = asyncio.create_task(stop_typing(event.chat_id))


@client.on(events.NewMessage(pattern="\\/clear_history"))
async def on_clear_history(event):  # TODO
    print(f"[{event.chat_id}] clear history command received")

    if event.is_group:
        await event.respond("I can't clear history in groups yet.")
        return  # ignore group messages


    global wait_tasks  # cancel any pending messages
    if event.chat_id in wait_tasks and wait_tasks[event.chat_id]:
        wait_tasks[event.chat_id].cancel()

    # await client(
    #     functions.messages.DeleteHistoryRequest(
    #         peer=event.chat_id, max_id=-1, revoke=True
    #     )
    # )

    clear_history(event.chat_id)
    delete_summary(event.chat_id)

    global current_message_ids, current_message_parts
    if event.chat_id in current_message_ids:
        del current_message_ids[event.chat_id]
    if event.chat_id in current_message_parts:
        del current_message_parts[event.chat_id]

    await event.respond("History cleared.")
    raise events.StopPropagation


@client.on(events.NewMessage(pattern="\\/update_history"))
async def on_update_history(event):
    # TODO: consider context of this function call:
    # are there pending messages from user?
    # is bot currently replying?

    # --
    print(f"[{event.chat_id}] update history command received")

    if event.is_group:
        await event.respond("I can't clear history in groups yet.")
        return  # ignore group messages
    
    my_id = (await client.get_me(input_peer=True)).user_id
    chat_id = event.chat_id
    min_id = get_history_min_id(chat_id)

    chat_log = []
    async for message in client.iter_messages(
        chat_id, min_id=min_id - 1, max_id=event.message.id
    ):  # ignore command message
        if message.from_id == None or message.from_id.user_id != my_id:
            chat_log.insert(0, ("user", message.text, message.id))
        else:
            chat_log.insert(0, ("bot", message.text, message.id))

    parsed_history = parse_history(chat_log)
    print(f"{my_id=}")
    print(f"{min_id=}")
    pprint(chat_log)
    pprint(parsed_history)
    rewrite_history(chat_id, parsed_history)
    # --

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

    # if event.is_group and not event.message.mentioned:
    #     # ignore group messages that are not mentioned
    #     return

    # message received, cancel task and immediately set typing status to False
    # so no need to wait for 6s, bot immediately starts to respond
    global cancel_tasks
    if chat_id in cancel_tasks and cancel_tasks[chat_id]:
        cancel_tasks[chat_id].cancel()

    global client_typing_statuses
    client_typing_statuses[chat_id] = False
    print(f"[{chat_id}] typing=False (message received)")

    #await event.mark_read()  # mark as read!

    # NOW THEN START to parse message received
    # global current_message_ids, current_message_parts
    global current_message_parts
    # if not chat_id in current_message_ids:
    #     # id of the entire message is that of the first message
    #     # (not updated on subsequent messages)
    #     # because all messages sent within this delay/"buffer" are combined into one, and fed to LLM together, so counted as 1
    #     current_message_ids[chat_id] = event.message.id

    if not chat_id in current_message_parts:
        current_message_parts[chat_id] = []

    sender = await event.get_sender()
    event_username = sender.username if sender and sender.username else "Unknown" 
    current_message_parts[chat_id].append({"message_id":event.message.id,
                                           "sender_name": event_username,
                                           "content": event.raw_text})
    print(f"[{chat_id}] current_message_parts length: {len(current_message_parts[chat_id])}")
    # start wait task before sending a reply (in case subsequent messages come in)
    # if msg keep comning in, then it wont reply until the user stops typing for DELAY seconds
    # wait tasks is only used here
    global wait_tasks
    # when task is done it will not be removed from wait_tasks, it will just become None, so need to check both
    # if chat_id in wait_tasks and wait_tasks[chat_id]:
    #     wait_tasks[chat_id].cancel()

    # if nothing there or task done:
    #     start
    # pass this step means there is a task and still running (waiting)
    # if len > threshold:
    #     cancel
    #     start
    #  (small chance that handler get called again while this task is still running)

    if chat_id not in wait_tasks or wait_tasks[chat_id].done():
        # If no wait task exists or the existing one is done, create a new one and overwrite the existing one
        # This will wait for DELAY seconds before replying
        delay = random.randint(1, MAX_OFFLINE_TIME)
        wait_tasks[chat_id] = asyncio.create_task(wait_before_reply(event,delay))

    # Decide whether to reply based on buffer threshold or set a timer
    if len(current_message_parts[chat_id]) >= INPUT_BUFFER_SIZE:
        # Buffer threshold reached, reply immediately
        wait_tasks[chat_id].cancel() # returns T or F
        wait_tasks[chat_id] = asyncio.create_task(wait_before_reply(event,0))



@client.on(events.ChatAction)
async def say_hi_added(event):
    """
    Send a "Hello" message when the bot is added to a group.
    """
    if event.user_added or event.user_joined:
        # Check if the bot itself was added to the group
        me = await client.get_me()
        if event.user_id == me.id:
            print(f"Bot added to group: {event.chat_id}")
            await event.respond("Hi Im Rachel! Nice to meet you!")
