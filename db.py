import sqlite3
from typing import Union
from prompts import SYSTEM_PROMPT

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
	"id"	INTEGER,
	"chat_id"	INTEGER,
	"input"	TEXT,
	"response"	TEXT,
	PRIMARY KEY("id" AUTOINCREMENT)
);
"""
DB_INSERT_SYSTEM_PROMPT_CMD = """
INSERT INTO "SystemPrompt" VALUES (?)
"""

db = sqlite3.connect(DB_PATH)
for cmd in DB_INIT.split(";"):
    db.execute(cmd)
db.execute(DB_INSERT_SYSTEM_PROMPT_CMD, (SYSTEM_PROMPT,))
db.commit()
db.close()

#  system prompt for the bot
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
            "SELECT input, response, id FROM History WHERE chat_id=? ORDER BY id ASC",
            (chat_id,),
        )
    else:  # count specified, limit response
        res = db.execute(
            "SELECT input, response, id FROM History WHERE chat_id=? ORDER BY id ASC LIMIT ?",
            (chat_id, count),
        )

    # convert to desired format
    out = [{"input": i[0], "response": i[1], "id": i[2]} for i in res]

    db.close()

    return out


def add_history(id_: int, chat_id: int, input_: str, response: str):
    """
    id_: message id of first message sent by user in this piece of history
    """

    db = sqlite3.connect(DB_PATH)
    db.execute(
        "INSERT INTO History(id, chat_id, input, response) VALUES (?,?,?,?)",
        (
            id_,
            chat_id,
            input_,
            response,
        ),
    )
    db.commit()
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
        "SELECT MIN(id) FROM History WHERE chat_id=?",
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
            "INSERT INTO History(id, chat_id, input, response) VALUES(?,?,?,?)",
            (item["id"], chat_id, item["input"], item["response"]),
        )

    db.commit()
    db.close()


def get_history_length(chat_id: int) -> int:
    db = sqlite3.connect(DB_PATH)

    res = db.execute("SELECT COUNT(id) FROM History WHERE chat_id=?", (chat_id,))
    count = res.fetchone()[0]

    db.close()

    return count


def delete_history(chat_id: int, ids: list[int]):
    """
    for summary purposes
    """
    db = sqlite3.connect(DB_PATH)

    for id_ in ids:
        db.execute(
            "DELETE FROM History WHERE chat_id=? AND id=?",
            (chat_id, id_),
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
