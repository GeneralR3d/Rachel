import sqlite3
from typing import Union
from prompts import SYSTEM_PROMPT
import traceback

system_prompt = None

DB_PATH = "data.db"

# initialise db if not already done
DB_INIT = f"""BEGIN TRANSACTION;
CREATE TABLE IF NOT EXISTS "SystemPrompt" (
	"SystemPrompt"	TEXT
);
CREATE TABLE IF NOT EXISTS "Summary" (
	"chat_id"	INTEGER,
	"summary"	TEXT,
	PRIMARY KEY("chat_id")
);
CREATE TABLE IF NOT EXISTS "History" (
	"message_id"	INTEGER,
	"chat_id"	INTEGER,
	"sender"	TEXT,
	"content"	TEXT,
	PRIMARY KEY("message_id" AUTOINCREMENT)
);
"""
# AUTOINCREMENT just means if not inserted manually, the message_id will be incremented automatically

DB_INSERT_SYSTEM_PROMPT_CMD = """
INSERT INTO "SystemPrompt" VALUES (?)
"""

db = sqlite3.connect(DB_PATH)
for cmd in DB_INIT.split(";"):
    db.execute(cmd)
db.execute(DB_INSERT_SYSTEM_PROMPT_CMD, (SYSTEM_PROMPT,))
db.commit()
db.close()

# system prompt for the bot
def get_system_prompt() -> str:
    global system_prompt
    if system_prompt:
        return system_prompt

    db = sqlite3.connect(DB_PATH)
    res = db.execute("SELECT SystemPrompt FROM SystemPrompt LIMIT 1")
    system_prompt = res.fetchone()[0]
    db.close()

    return system_prompt

# system_prompt is system prompt for the bot
def set_system_prompt(new_system_prompt):
    global system_prompt
    system_prompt = new_system_prompt

    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE SystemPrompt SET SystemPrompt=?", (new_system_prompt,))
    db.commit()
    db.close()


def get_history(chat_id: int, count: int = -1):
    """
    count: number of pieces to get (starting from earliest), -1 for all
    ASC order is used to get the history in the order it was added
    chat_id: id of the chat for which to get the history
    """

    db = sqlite3.connect(DB_PATH)
    if count == -1:
        res = db.execute(
            "SELECT sender, content, message_id FROM History WHERE chat_id=? ORDER BY message_id ASC",
            (chat_id,),
        )
    else:  # count specified, limit response
        res = db.execute(
            "SELECT sender, content, message_id FROM History WHERE chat_id=? ORDER BY message_id ASC LIMIT ?",
            (chat_id, count),
        )

    # convert to desired format
    out = [{"sender": i[0], "content": i[1], "message_id": i[2]} for i in res]

    db.close()

    return out

def add_history(chat_id: int, sender: str, content: str, message_id: int = None):
    """
    Adds a single history entry to the database.
    
    Parameters:
    - chat_id: chat ID where the message was sent
    - sender: who sent the message
    - content: the message content
    - message_id: message ID of the message (optional)
    """
    db = sqlite3.connect(DB_PATH)
    if message_id:
        db.execute(
            "INSERT INTO History(message_id, chat_id, sender, content) VALUES (?,?,?,?)",
            (message_id, chat_id, sender, content)
        )
    else:
        db.execute(
            "INSERT INTO History(chat_id, sender, content) VALUES (?,?,?)",
            (chat_id, sender, content)
        )

    db.commit()
    db.close()

def add_history_batch(chat_ids: list[int], senders: list[str], contents: list[str], 
                      message_ids: list[int] = None):
    """
    Adds multiple history entries to the database.
    It also works when the list is of length 1, i.e. a single message.
    
    Must be called with lists of equal length:
    - chat_ids: list of chat IDs where the messages were sent
    - senders: list of who sent the messages
    - contents: list of message contents
    - message_ids: list of message IDs (optional)
    """
    db = sqlite3.connect(DB_PATH)
    
    try:
        # Ensure input lists have the same length
        if not len(chat_ids) == len(senders) == len(contents):
            raise ValueError("chat_ids, senders, and contents must be lists of the same length")
        
        # If message_ids is provided, ensure it has the same length
        if message_ids is not None and len(message_ids) != len(chat_ids):
            raise ValueError("message_ids must have the same length as other lists")
        
        if message_ids is not None:
            # Use message_ids
            data = list(zip(message_ids, chat_ids, senders, contents))
            db.executemany(
                "INSERT INTO History(message_id, chat_id, sender, content) VALUES (?,?,?,?)",
                data
            )
        else:
            # Skip message_ids, let SQLite auto-increment
            data = list(zip(chat_ids, senders, contents))
            db.executemany(
                "INSERT INTO History(chat_id, sender, content) VALUES (?,?,?)",
                data
            )
        
        db.commit()
    except sqlite3.Error as e:
        print("Database error occurred:", e)
        traceback.print_exc()
    except Exception as e:
        print("An unexpected error occurred:", e)
        traceback.print_exc()
    finally:
        db.close()


def clear_history(chat_id: int):
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "DELETE FROM History WHERE chat_id=?",
        (chat_id,),
    )
    db.commit()
    db.close()


def get_history_min_id(chat_id: int) -> int:
    """
    get the min message id for the history of chat_id
    used when trying to update the history that is stored in the database
    to avoid having to retrieve all messages from telegram
    """

    db = sqlite3.connect(DB_PATH)
    res = db.execute(
        "SELECT MIN(message_id) FROM History WHERE chat_id=?",
        (chat_id,),
    )
    out = res.fetchone()
    db.close()

    # return 0 if no min id i.e. no messages are stored in db rn
    return out[0] if out else 0


def rewrite_history(chat_id: int, parsed_history: list[dict]):
    db = sqlite3.connect(DB_PATH)

    db.execute("DELETE FROM History WHERE chat_id=?", (chat_id,))

    for item in parsed_history:
        db.execute(
            "INSERT INTO History(message_id, chat_id, sender, content) VALUES(?,?,?,?)",
            (item["message_id"], chat_id, item["sender"], item["content"]),
        )

    db.commit()
    db.close()


def get_history_length(chat_id: int) -> int:
    db = sqlite3.connect(DB_PATH)

    res = db.execute("SELECT COUNT(message_id) FROM History WHERE chat_id=?", (chat_id,))
    count = res.fetchone()[0]

    db.close()

    return count


def delete_history(chat_id: int, message_ids: list[int]):
    """
    for summary purposes
    """
    db = sqlite3.connect(DB_PATH)

    for message_id in message_ids:
        db.execute(
            "DELETE FROM History WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
    db.commit()
    db.close()


def get_summary(chat_id: int) -> Union[str, None]:
    db = sqlite3.connect(DB_PATH)
    res = db.execute("SELECT summary FROM Summary WHERE chat_id=?", (chat_id,))
    summary = res.fetchone()
    db.close()

    return summary[0] if summary else None


def set_summary(chat_id: int, summary: str):
    """actually an upsert"""
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "INSERT INTO Summary(chat_id, summary) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET summary=?",
        (chat_id, summary, summary),
    )
    db.commit()
    db.close()


def delete_summary(chat_id: int):
    db = sqlite3.connect(DB_PATH)
    db.execute(
        "DELETE FROM Summary WHERE chat_id=?",
        (chat_id,),
    )
    db.commit()
    db.close()
