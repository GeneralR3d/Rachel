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

Copy `template.env` to `.env` and fill in the required values before running. Rachel's own Telegram account credentials are entered interactively during `scripts.login` and saved to `anon.session` — they are never in `.env`.

## Architecture

Rachel is a FastAPI app that runs two Telethon clients on the same asyncio event loop as uvicorn — there is no webhook.

**Startup flow** (`app/main.py`): lifespan seeds both system prompts and personality traits into Postgres, then starts both Telegram clients. Shutdown flushes all in-memory message buffers to DB before disconnecting.

**Rachel client** (`app/telegram/client.py`): the core message loop. Per-chat state lives in module-level dicts (`current_messages_buffer`, `wait_tasks`, `flush_tasks`, `last_message_time`). On the first message for a chat, the buffer is seeded from the last `N_PAST_MSG_REQUIRED = 20` DB rows (marked `is_persisted=True`). Each new message cancels and reschedules two asyncio tasks:
- **Reply task** (`wait_tasks`): fires `REPLY_DELAY = 5 s` after the last message. `reply()` slices the last 20 messages from the buffer, calls `get_response`, sends the response as `\n\n`-separated paragraphs with a simulated typing delay, persists a new summary if the summarizer produced one, and appends the bot's reply to the buffer as `is_persisted=False`.
- **Flush task** (`flush_tasks`): fires `CHAT_BLACKOUT_TIME = 180 s` after the last message. Writes all `is_persisted=False` entries via `add_history_batch`, then clears the chat's buffer (next message re-seeds from DB). Also triggered immediately when the buffer reaches `MAX_BUFFER_LEN = 150`.

**Admin bot** (`app/telegram/bot.py`): a separate `TelegramClient("bot", ...)` that only responds to `ADMIN_ID`. Commands: `/get_responder_system_prompt`, `/set_responder_system_prompt`, `/list_user_names`, `/list_chats`, `/get_history <chat_id>`, `/clear_history <chat_id>`, `/get_summary <chat_id>`, `/delete_summary <chat_id>`, `/list_traits`, `/set_trait <id> <low|medium|high>`, `/reset_traits`.

**LLM service** (`app/services/gemini.py`): uses OpenRouter (OpenAI-compatible) via `langchain-openrouter`, despite the filename. A LangGraph graph is compiled once at module import into `_graph` and reused for every call.

The graph has two nodes that fan out from START and run **in parallel**:

```
START → summarizer_node ↘
      → responder_node  → END
```

- **`summarizer_node`**: reads `SUMMARIZER_SYSTEM_PROMPT` from DB, injects `{old_summary}` and `{mood_list}` via `ChatPromptTemplate`, passes history as `human`/`assistant` turns, returns `SummarizerOutput` (mood + summary). If summary is `"NIL"`, only `mood` is written back to state (leaving `current_summary` unchanged). Otherwise both are written.
- **`responder_node`**: reads `RESPONDER_SYSTEM_PROMPT` from DB, injects `{examples_text}` (tone examples for the *previous* call's detected mood) and `{current_summary}` via `ChatPromptTemplate`, passes history as `human`/`assistant` turns, returns `ResponseOutput` (sender + content).

Mood detected by `summarizer_node` is stored in the module-level `_chat_mood: Dict[int, str]` dict and injected into `responder_node` on the **next** call (defaults to `"default"` on first contact). This one-call lag is intentional — both nodes run in parallel so the summarizer's mood can't influence the current responder.

`get_response()` returns `(response_text, new_summary | None, elapsed_seconds)`. `new_summary` is `None` when the summarizer returned NIL (no DB write needed).

**Prompt template injection**: Both system prompts contain `{variable}` placeholders filled by `ChatPromptTemplate.format_messages()` at call time. Variables: `RESPONDER_SYSTEM_PROMPT` uses `{examples_text}`, `{current_summary}`, and `{personality_traits}`; `SUMMARIZER_SYSTEM_PROMPT` uses `{mood_list}` and `{old_summary}`. History messages are formatted as `"SenderName: content"` for `human` turns; Rachel's own messages use just the content under the `assistant` role.

**Mood / tone system** (`app/prompts.py`): `CONVERSATION_TONE_TEMPLATES` is a dict keyed by mood name (e.g. `"default"`, `"excited"`, `"sad"`, `"flirt"`). Each value is a list of `{input, response}` example pairs that are formatted into `{examples_text}` to steer Rachel's tone. `MOOD_LABELS = list(CONVERSATION_TONE_TEMPLATES)` is the single source of truth for valid mood values — the summarizer's structured-output schema is built from it dynamically.

**Admin HTTP API** (`app/routers/admin.py`): mirrors the bot commands over REST. Endpoints: `GET/PUT /system-prompt`, `GET /users/names`, `GET /list-chats`, `GET/DELETE /history/{chat_id}`, `GET/DELETE /summary/{chat_id}`, `GET /personality`, `PATCH /personality/{trait_id}`, `POST /personality/reset`, `GET /health`.

**Data layer** (`app/repository.py`): all DB functions, each opening its own session via `session_scope`. Both system prompts are cached in module-level globals (`_responder_system_prompt`, `_summarizer_system_prompt`) — setters update both cache and Postgres. `get_history()` joins `History` with `User` to resolve `sender_user_id` into a display name (`username` → `first_name` → stringified ID).

**Personality traits**: stored in `personality_traits`. Each row has `low_prompt`/`medium_prompt`/`high_prompt` and `current_value`. Defaults are in `DEFAULT_TRAITS` in `app/prompts.py` and seeded on startup. `get_active_trait_prompts()` assembles the active prompt block (one `- Name: active_prompt` line per trait, ordered by `sort_order`) and is injected into `responder_node` as `{personality_traits}`. Its result is cached in module globals (`_active_trait_prompts_cache`/`_active_trait_prompts_cache_time`) for `TRAIT_CACHE_TTL = 5 min` since it's read on every message; `set_trait_value`/`reset_traits` call `_invalidate_trait_prompt_cache()` so admin edits take effect immediately rather than waiting out the TTL.

**Weekly schedule** (`app/models.py::ScheduleActivity`, `app/schedule_data.py`): one row per activity *instance*, keyed by `(day_of_week 0=Mon..6=Sun, start_hour 0-23)` with a unique constraint — `duration_hours` lets a single row span multiple hour-chunks (e.g. `start_hour=23, duration_hours=8` covers 23:00-06:59) rather than duplicating rows per hour. `DEFAULT_SCHEDULE` in `app/schedule_data.py` is the seed data, loaded by `ensure_schedule_seeded()` (insert-if-empty, called from the lifespan alongside the other seeders). Repository queries: `get_current_activity(day_of_week, hour)` (also checks the *previous* day for activities that run past midnight), `get_day_summary(day_of_week)` (name/duration_hours/location only, via `_activity_to_dict(a, partial=True)`).

**Config** (`app/config.py`): pydantic-settings with `@lru_cache`. `DATABASE_URL` must use `localhost:5433` locally (Docker port-mapped) and `db:5432` inside Docker.

The original Telethon/SQLite implementation is preserved under `Reference/` — `app/repository.py` is a deliberate port of `Reference/app/db.py` with matching function names/signatures, so consult it when porting further pieces.

## Known gaps

- There is a race condition in `_flush_chat`: messages arriving between the flush write and the buffer clear are silently dropped. Marked with a `#TODO` in `client.py`.
- `summarise_text()` in `gemini.py` is stubbed — the `summarise()` function in `client.py` (which trims old history and compresses it) does not yet work.
