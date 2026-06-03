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

## Architecture

Rachel is a FastAPI app that runs two Telethon clients on the same asyncio event loop as uvicorn — there is no webhook.

**Startup flow** (`app/main.py`): lifespan seeds the default system prompt, then starts both Telegram clients and serves the HTTP API at port 8000.

**Rachel client** (`app/telegram/client.py`): buffers incoming messages per `chat_id` in module-level dicts (`current_message_parts`, `wait_tasks`). On a randomised delay (1–10 s) or when the buffer hits 15 messages, `reply()` fetches history + summary, calls Gemini, saves both sides to Postgres, and sends each `\n\n`-separated paragraph with a fake typing delay. When history exceeds 500 rows a background `summarise()` task fires — **note**: this function has a pre-existing key mismatch (`id`/`input`/`response` vs `sender`/`content`/`message_id`) that is preserved from the reference.

**Admin bot** (`app/telegram/bot.py`): a separate bot token; only responds to the `ADMIN_ID`. Exposes `/get_system_prompt` and `/set_system_prompt` commands.

**Gemini service** (`app/services/gemini.py`): the blocking `generate_content` SDK call is offloaded via `asyncio.to_thread`. Responds with structured JSON (`llm_response` schema) parsed to `.content`. `summarise_text` is currently stubbed.

**Data layer**: `app/repository.py` contains all DB functions. Each call opens its own session via `session_scope` (from `app/database.py`). `get_system_prompt()` caches the result in a module-level global; `set_system_prompt()` updates both the cache and Postgres.

**Config**: all settings are in `app/config.py` (pydantic-settings, `@lru_cache`). Loaded from `.env`. `DATABASE_URL` must use `localhost:5433` when running the app locally against the dockerized DB (port mapped in `docker-compose.yml`), and `db:5432` when running inside Docker.

## Key known issue

`summarise()` in `app/telegram/client.py` reads history items with keys `id`/`input`/`response`, but `get_history()` returns `sender`/`content`/`message_id`. Summarisation (triggered only past `HISTORY_LENGTH_THRESHOLD = 500`) is broken until this is reconciled.
