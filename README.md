# Rachel

LLM-based conversational Telegram bot that mimics human texting behaviour. Rachel is a Singapore girl!

This is a **FastAPI** re-platform of the original Telethon/SQLite bot: both Telegram clients run inside the FastAPI lifespan, conversation state lives in **Postgres** (async SQLAlchemy + asyncpg), schema is managed with **Alembic**, and dependencies are managed with **uv**. The original reference implementation is preserved under [`Reference/`](Reference/).

## Architecture

- **Rachel client** (`app/telegram/client.py`) — the bot your friends talk to. Buffers incoming messages, waits a randomised delay, calls Gemini, and replies with fake typing delays. Session: `anon.session`.
- **Admin bot** (`app/telegram/bot.py`) — only you talk to this; `/get_responder_system_prompt` and `/set_responder_system_prompt` manage Rachel's personality. Session: `bot.session`.
- **Admin HTTP API** (`app/routers/admin.py`) — manage the same state over HTTP.
- **Gemini service** (`app/services/gemini.py`) — generates replies (blocking SDK call offloaded to a thread).
- **Data layer** (`app/models.py`, `app/repository.py`, `app/database.py`) — async Postgres access.

Telethon keeps a persistent connection to Telegram — there is **no webhook**.

## Requirements

- Python ≥ 3.11 (managed by uv)
- [uv](https://docs.astral.sh/uv/)
- Docker (for Postgres)

## Credentials

| Credential | What it is | How to get it |
|---|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Identify your app to Telegram | [my.telegram.org](https://my.telegram.org) |
| Rachel's bot token | Logs the app in as Rachel (entered once, interactively) | [@BotFather](https://t.me/botfather) |
| `TELEGRAM_BOT_TOKEN` | Logs in the separate admin bot | A second bot via [@BotFather](https://t.me/botfather) |
| `GEMINI_API_KEY` | Access to the AI | [Google AI Studio](https://aistudio.google.com/apikey) |
| `ADMIN_ID` | Your personal Telegram user ID | [@userinfobot](https://t.me/userinfobot) |

`BOT_NAME` and `USER_NAME` are optional (`BOT_NAME` defaults to `Rachel`).

## Setup

```bash
# 1. Install dependencies
uv sync

# 2. Configure environment
cp template.env .env
# ...then fill in .env

# 3. Start Postgres
docker compose up -d db

# 4. Create the schema
uv run alembic upgrade head

# 5. One-time: log Rachel in (creates anon.session)
#    When prompted, paste RACHEL'S bot token (not the admin token).
uv run python -m scripts.login

# 6. Run the app
uv run uvicorn app.main:app --reload
```

On startup the app seeds the default system prompt (only if not already present), starts both Telegram clients, and serves the HTTP API at `http://localhost:8000`.

## Admin bot commands

Sent as Telegram messages to the admin bot (`bot.session`); only `ADMIN_ID` is allowed.

| Command | Description |
|---|---|
| `/get_responder_system_prompt` | Show the current responder system prompt |
| `/set_responder_system_prompt <text>` | Set a new responder system prompt |
| `/get_summarizer_system_prompt` | Show the current summarizer system prompt |
| `/set_summarizer_system_prompt <text>` | Set a new summarizer system prompt |
| `/list_user_names` | List all known usernames and names |
| `/list_chats` | List all chats with message counts |
| `/get_history <chat_id>` | Show stored history for a chat (includes responder `reason` lines) |
| `/clear_history <chat_id>` | Clear a chat's history |
| `/get_summary <chat_id>` | Show a chat's conversation summary |
| `/delete_summary <chat_id>` | Delete a chat's conversation summary |
| `/get_user_facts <user_id>` | Show stored facts/preferences for a user |
| `/set_user_facts <user_id> <facts text>` | Set facts/preferences for a user |
| `/delete_user_facts <user_id>` | Delete a user's stored facts/preferences |
| `/list_traits` | List all personality trait sliders and current values |
| `/set_trait <id> <low\|medium\|high>` | Set one personality trait |
| `/reset_traits` | Reset all personality traits to `medium` |

## Admin HTTP API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/system-prompt` | Current system prompt |
| `PUT` | `/system-prompt` | Set prompt — body `{"prompt": "..."}` |
| `GET` | `/history/{chat_id}` | Stored conversation history |
| `DELETE` | `/history/{chat_id}` | Clear a chat's history |
| `GET` | `/summary/{chat_id}` | Stored summary |
| `DELETE` | `/summary/{chat_id}` | Delete a chat's summary |
| `GET` | `/users/names` | All known Telegram users |
| `GET` | `/list-chats` | All chats with message counts |
| `GET` | `/personality` | All personality trait sliders with current values |
| `PATCH` | `/personality/{id}` | Set one trait — body `{"value": "low"\|"medium"\|"high"}` |
| `POST` | `/personality/reset` | Reset all traits to `medium` |

To set a per-chat scope, you need to use the Bot API directly instead of BotFather. Send a setMyCommands request with the scope field:

```  
  curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setMyCommands" \
    -H "Content-Type: application/json" \
    -d '{
      "commands": [
        {"command": "get_responder_system_prompt", "description": "Get the current responder system prompt"},
        {"command": "set_responder_system_prompt", "description": "Set a new responder system prompt"},
        {"command": "get_summarizer_system_prompt", "description": "Get the current summarizer system prompt"},
        {"command": "set_summarizer_system_prompt", "description": "Set a new summarizer system prompt"},
        {"command": "list_chats", "description": "List all chats with message counts"},
        {"command": "get_history", "description": "Get message history for a chat"},
        {"command": "clear_history", "description": "Clear message history for a chat"},
        {"command": "get_summary", "description": "Get the conversation summary for a chat"},
        {"command": "delete_summary", "description": "Delete the conversation summary for a chat"},
        {"command": "list_user_names", "description": "List all usernames and names"},
        {"command": "get_user_facts", "description": "Get stored facts/preferences for a user: /get_user_facts <user_id>"},
        {"command": "set_user_facts", "description": "Set facts/preferences for a user: /set_user_facts <user_id> <facts text>"},
        {"command": "delete_user_facts", "description": "Delete stored facts/preferences for a user: /delete_user_facts <user_id>"},
        {"command": "list_traits", "description": "List all personality trait sliders and current values"},
        {"command": "set_trait", "description": "Set a trait value: /set_trait <id> <low|medium|high>"},
        {"command": "reset_traits", "description": "Reset all personality traits to medium"}
      ],    
      "scope": {
        "type": "chat",
        "chat_id": <YOUR_ADMIN_ID>
      }
    }'
```

Interactive docs at `http://localhost:8000/docs`.

## How it works

Incoming messages are buffered per chat; after a short randomised delay (or once the buffer fills) the full history + any summary is sent to Gemini, the reply is split on blank lines and sent back with per-message typing delays, and both sides of the exchange are written to Postgres. When a chat's history grows past a threshold, the oldest messages are summarised and the summary is appended to Rachel's system prompt. See [`Reference/README.md`](Reference/README.md) for the original deep-dive on message flow.

## Database schema

| Table | Columns |
|---|---|
| `system_prompt` | `id`, `system_prompt` (single row) |
| `summary` | `chat_id` (PK), `summary` |
| `history` | `chat_id` + `telegram_message_id` (composite PK), `sender_user_id`, `content`, `created_at` |
| `users` | `telegram_user_id` (PK), `first_name`, `last_name`, `username`, `created_at`, `updated_at` |
| `personality_traits` | `id` (PK), `name`, `sort_order`, `low_prompt`, `medium_prompt`, `high_prompt`, `current_value` |

## Migrations

```bash
uv run alembic upgrade head          # apply
uv run alembic downgrade -1          # roll back one
uv run alembic revision -m "msg"     # new migration
```

## Known issue carried over from the reference

`app/telegram/client.py::summarise()` reads history items with keys
`id`/`input`/`response`, but `get_history()` returns `sender`/`content`/`message_id`.
This mismatch exists in the original code and is preserved verbatim; summarisation
(triggered only past `HISTORY_LENGTH_THRESHOLD = 500`) would need this reconciled to work.
