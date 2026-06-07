# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the app (requires Postgres running and anon.session present)
uv run uvicorn app.main:app --reload

# One-time login: create anon.session for Rachel's client
uv run python -m scripts.login

# Database migrations
uv run alembic upgrade head          # apply all
uv run alembic downgrade -1          # roll back one
uv run alembic revision -m "msg"     # generate new migration

# Start Postgres only
docker compose up -d db
```

Copy `template.env` to `.env` and fill in the required values before running.

## Architecture

Rachel is a FastAPI app that runs two Telethon clients on the same asyncio event loop as uvicorn — there is no webhook.

**Startup flow** (`app/main.py`): lifespan seeds the default system prompt and personality traits, then starts both Telegram clients and serves the HTTP API at port 8000.

**Rachel client** (`app/telegram/client.py`): the core message loop. Per-chat state lives in module-level dicts. On the first message for a chat, `current_messages_buffer` is initialised by fetching the last `N_PAST_MSG_REQUIRED = 20` messages from the DB (marked `is_persisted=True`). Every subsequent incoming message is appended as `is_persisted=False`. Each new message cancels and reschedules two tasks:
- **Reply task** (`wait_tasks`): fires `REPLY_DELAY = 3 s` after the last message. `reply()` slices the last `N_PAST_MSG_REQUIRED` messages from the buffer and passes them as `history` to `get_response` (empty `current_messages`), sends the response as `\n\n`-separated paragraphs with a fake typing delay, then appends the bot's response to the buffer as `is_persisted=False`.
- **Flush task** (`flush_tasks`): fires `CHAT_BLACKOUT_TIME = 60 s` after the last message. `flush_buffer()` writes all `is_persisted=False` entries via `add_history_batch`, then removes the chat's buffer entry entirely (next message re-seeds from DB).

**Admin bot** (`app/telegram/bot.py`): a separate `TelegramClient("bot", ...)` that only responds to `ADMIN_ID`. Commands: `/get_responder_system_prompt`, `/set_responder_system_prompt`, `/list_user_names`, `/list_chats`, `/get_history <chat_id>`, `/clear_history <chat_id>`, `/get_summary <chat_id>`, `/delete_summary <chat_id>`, `/list_traits`, `/set_trait <id> <low|medium|high>`, `/reset_traits`.

**LLM service** (`app/services/gemini.py`): uses OpenRouter (OpenAI-compatible API) despite the filename. `get_response(current_messages, history, summary)` builds a message list where role is `"assistant"` when `entry["sender"] == BOT_NAME`, else `"user"`; `current_messages` entries are always appended as `"user"`. Requests `json_object` format and parses `data["content"]`. `summarise_text` is currently stubbed.

**Admin HTTP API** (`app/routers/admin.py`): mirrors the bot commands over REST. Endpoints: `GET/PUT /system-prompt`, `GET /users/names`, `GET /list-chats`, `GET/DELETE /history/{chat_id}`, `GET/DELETE /summary/{chat_id}`, `GET /personality`, `PATCH /personality/{trait_id}`, `POST /personality/reset`, `GET /health`.

**Data layer**: `app/repository.py` contains all DB functions. Each call opens its own session via `session_scope` (from `app/database.py`). `get_responder_system_prompt()` caches in a module-level global; `set_responder_system_prompt()` updates both the cache and Postgres. `get_history(chat_id, count)` joins `History` with `User` to resolve `sender_user_id` into a display name (`username` → `first_name` → stringified ID), returning dicts with keys `sender`, `sender_user_id`, `content`, `telegram_message_id`, `chat_id`.

**Personality traits**: stored in `personality_traits`. Each row has `low_prompt`/`medium_prompt`/`high_prompt` and a `current_value`. Default definitions are in `app/prompts.py` (`DEFAULT_TRAITS`) and seeded on startup. `get_active_trait_prompts()` assembles the active prompt block for injection into the system prompt.

**Config**: all settings in `app/config.py` (pydantic-settings, `@lru_cache`). `DATABASE_URL` must use `localhost:5433` when running locally against the dockerised DB (port mapped in `docker-compose.yml`), and `db:5432` inside Docker.
