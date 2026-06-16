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

There is no test suite or lint config in this repo.

## Architecture

Rachel is a FastAPI app that runs two Telethon clients on the same asyncio event loop as uvicorn — there is no webhook.

**Startup flow** (`app/main.py`): lifespan seeds both system prompts, personality traits, and the weekly schedule into Postgres, then starts both Telegram clients. Shutdown flushes all in-memory message buffers to DB before disconnecting.

**Rachel client** (`app/telegram/client.py`): the core message loop. Per-chat state lives in module-level dicts (`current_messages_buffer`, `wait_tasks`, `flush_tasks`, `last_message_time`). On the first message for a chat, the buffer is seeded from the last `N_PAST_MSG_REQUIRED = 20` DB rows (marked `is_persisted=True`). Each new message cancels and reschedules two asyncio tasks:
- **Reply task** (`wait_tasks`): fires `REPLY_DELAY = 7 s` after the last message. `reply()` slices the last 20 messages from the buffer, computes the distinct `sender_user_ids` in that slice (passed to `get_response` so the responder can load per-user facts), calls `get_response`, sends the response as `\n\n`-separated paragraphs with a simulated typing delay, persists a new summary if the summarizer produced one, and appends the bot's reply to the buffer as `is_persisted=False` along with the responder's `reason`.
- **Flush task** (`flush_tasks`, via `finalize_conversation`): fires `CHAT_BLACKOUT_TIME = 60 s` after the last message. Treats the chat as "conversation finished": snapshots the buffer (plus its summary, from `pending_summaries` or falling back to the DB), calls `_flush_chat` (writes all `is_persisted=False` entries via `add_history_batch`, including each message's `reason`, then clears the chat's buffer — next message re-seeds from DB), and fires off **both** `update_worldview()` and `update_user_facts()` as detached tasks on the snapshotted conversation. Also triggered immediately when the buffer reaches `MAX_BUFFER_LEN = 150` — this cap is now enforced in `new_message` on **every** incoming message (via `finalize_conversation(chat_id, 0)`, so it *does* run both memory pipelines), not in `reply()`, because in group chats Rachel only replies when tagged and a busy untagged group would otherwise reset the flush timer forever and grow the buffer unbounded.

**Reply gating** (`new_message`): Rachel buffers and flushes *every* incoming message, but only schedules a reply when `event.is_private or event.mentioned` — i.e. always in 1-on-1 DMs, and in group chats only when she's @-mentioned or someone replies to one of her messages.

**Admin bot** (`app/telegram/bot.py`): a separate `TelegramClient("bot", ...)` that only responds to `ADMIN_ID`. Commands: `/get_responder_system_prompt`, `/set_responder_system_prompt`, `/get_summarizer_system_prompt`, `/set_summarizer_system_prompt`, `/list_user_names`, `/list_chats`, `/get_history <chat_id>`, `/clear_history <chat_id>`, `/get_summary <chat_id>`, `/delete_summary <chat_id>`, `/list_traits`, `/set_trait <id> <low|medium|high>`, `/reset_traits`, `/get_user_facts <user_id>`, `/set_user_facts <user_id> <facts text>`, `/delete_user_facts <user_id>`. A broad `/set_trait...` fallback handler replies with usage when args don't match `<id> <low|medium|high>`; the specific handler `raise events.StopPropagation` after replying so both don't fire on a valid command.

**LLM service** (`app/services/llm.py`): uses OpenRouter (OpenAI-compatible) via `langchain-openrouter`. A LangGraph graph is compiled once at module import into `_graph` and reused for every call.

The graph has two nodes that fan out from START and run **in parallel**:

```
START → summarizer_node ↘
      → responder_node  → END
```

- **`summarizer_node`**: reads `SUMMARIZER_SYSTEM_PROMPT` from DB, injects `{old_summary}` and `{mood_list}` via `ChatPromptTemplate`, passes history as `human`/`assistant` turns, returns `SummarizerOutput` (mood + summary). If summary is `"NIL"`, only `mood` is written back to state (leaving `current_summary` unchanged). Otherwise both are written. LLM errors here are swallowed (keeps current mood/summary) rather than retried.
- **`responder_node`**: reads `RESPONDER_SYSTEM_PROMPT` from DB and injects, via `ChatPromptTemplate`: `{communication_style}` (tone examples for the *previous* call's detected mood, from `CONVERSATION_STYLE`), `{current_summary}`, `{personality_traits}`, `{conversation_mood}`, `{datetime}` (formatted current date/time), `{current_activity}` and `{day_summary}` (from the weekly schedule), `{world_view}` (from `read_worldview()`), and `{user_facts}` (per-user facts for every distinct `sender_user_id` in the slice, fetched via `_get_user_facts_cached`). History is passed as `human`/`assistant` turns. Returns `ResponseOutput` (`content` + `reason`, the latter a one-sentence justification persisted to `History.reason` for traceability/debugging). Uses `json_mode` instead of tool-calling because tool-calling hangs on the configured model.

Mood detected by `summarizer_node` is stored in the module-level `_chat_mood: Dict[int, str]` dict and injected into `responder_node` on the **next** call (defaults to `"default"` on first contact). This one-call lag is intentional — both nodes run in parallel so the summarizer's mood can't influence the current responder.

`get_response()` returns `(response_text, response_reason, new_summary | None, elapsed_seconds)`. `new_summary` is `None` when the summarizer returned NIL (no DB write needed).

Schedule lookups (`current_activity`, `day_summary`) are cached in module globals (`_current_activity_cache` keyed on `(day_of_week, hour)`, `_day_summary_cache` keyed on `day_of_week`) since they only change once per hour/day and are read on every message.

**Mood / tone system** (`app/prompts.py`): `CONVERSATION_STYLE` is a dict keyed by mood name (e.g. `"default"`, `"excited"`, `"sad"`, `"flirt"`). Each value is formatted into `{communication_style}` to steer Rachel's tone. `MOOD_LABELS = list(CONVERSATION_STYLE)` is the single source of truth for valid mood values — the summarizer's structured-output schema is built from it dynamically.

**World view / persistent memory** (`app/services/worldview.py`): a separate, self-contained LangGraph pipeline that runs once per finished conversation (triggered from `finalize_conversation` in `client.py`). Two nodes run **sequentially**:

```
START → fact_extractor_node → (any new facts?) → consolidation_node → END
                                     └── no ──→ END
```

- **`fact_extractor_node`**: reads the just-finished dialogue with `FACT_EXTRACTOR_SYSTEM_PROMPT`, pulls out new durable, general (non-user-specific) facts about the world. Returns nothing if it learned nothing new — short-circuits straight to `END`, no file write.
- **`consolidation_node`**: reads existing facts plus newly extracted ones via `CONSOLIDATION_SYSTEM_PROMPT`, de-duplicates and resolves conflicts (newer info wins), returns the full rewritten fact set.

Storage is a flat markdown file (one fact per `- ` bullet) at `WORLDVIEW_PATH` (default `worldview.md`), read/rewritten as a whole each time — no search/retrieval. Reads/writes are serialized via a module-level `asyncio.Lock` (`_file_lock`) so concurrent conversation finalizations can't clobber each other. `read_worldview()` is consumed by `responder_node` (`{world_view}`); `update_worldview()` is the entry point called by `client.py` and never raises (errors are caught and logged so memory upkeep can't crash the caller).

**Per-user facts / preferences** (`app/services/userfacts.py`): the user-specific counterpart to the world-view pipeline, also run once per finished conversation from `finalize_conversation` (it takes the same snapshotted conversation plus its summary). Where world-view keeps *general* facts, this keeps *personal* memory about each individual user, stored per-user in the `user_facts_preferences` table (not a file). That row has **two independent kinds of memory**: the free-form `facts` text column (open-ended bullet list) and a fixed-slot `profile` JSONB column (generation, life stage, food vibe, …). Two **independent branches fan out from START in parallel** — one for facts, one for the profile — reading the same dialogue but writing to different columns, then joining at END. The facts branch splits extraction from a per-user LLM consolidation fan-out; the profile branch does extraction **and** the write in a single node (its merge is deterministic, so no consolidation pass or fan-out is needed):

```
START → fact_extractor_node → (any new facts?) → consolidation_node (×N, one per user, parallel) → END
      →                              └── no ──→ END
      → profile_extraction_update_node ──────────────────────────────────────────────────────────→ END
```

- **`fact_extractor_node`**: reads the dialogue once with `USER_FACT_EXTRACTOR_SYSTEM_PROMPT` (which also gets `{chat_summary}` and `{observation_date}` for context) and returns new durable personal facts **grouped by the sender they're about**. The model only ever sees/emits sender *names* (less hallucination-prone than ids); names are resolved back to numeric ids via a `name_to_id` map built in `update_user_facts()`, and any unrecognized name (or Rachel herself) is dropped. Short-circuits to `END` if nothing new.
- **`consolidation_node`**: fanned out via LangGraph `Send`, one instance per user with new facts. Each reads that user's stored facts, merges the new facts with `USER_FACT_CONSOLIDATION_SYSTEM_PROMPT` (de-dup + conflict resolution, newer wins) via an LLM call, and writes it back. A per-user `asyncio.Lock` (`_user_locks`, single-process only) guards the read-modify-write so concurrent finalizations touching the same user can't clobber each other.
- **`profile_extraction_update_node`**: a single node that does both extraction and the write (no separate consolidation node, no `Send` fan-out). It first reads every participant's **current** profile from the responder's cache (`get_user_profiles_cached`, public wrapper over `llm._get_user_profiles_cached`) and injects it into the prompt as `{existing_profiles}` (rendered by `_render_existing_profiles`, keyed by sender name, listing filled slots and calling out the empty ones) — this is context only, so cache staleness can only nudge what the model sees, never what gets persisted. It then reads the dialogue once with `USER_PROFILE_EXTRACTOR_SYSTEM_PROMPT` (also gets `{chat_summary}` and `{slot_descriptions}`), fills the fixed profile slots per sender (leaving slots with no evidence empty, and skipping already-known unchanged slots thanks to the injected baseline), and resolves names→ids / short-circuits like the facts branch. For each user with new slots it then read-modify-writes their profile **from the DB** under the per-user `_user_locks` (the authoritative read, distinct from the cached context read), merging by **deterministic code-level field overwrite** (newer non-empty slot wins; unknown keys dropped) — no LLM call, since each slot is a single standing value rather than an accumulating list. The slot schema is defined **once** in `USER_PROFILE_FIELDS` (`app/prompts.py`, a list of `(key, label, guidance)`); the extractor's structured-output model `UserProfileFields` is built dynamically from it via `pydantic.create_model`, and the responder's rendering + admin display reuse the same list — so adding/removing a slot needs no migration (it's one JSONB blob). Logs are tagged `[userprofile]` (via `_profile_tag`) to distinguish them from the facts branch's `[userfacts]`.

`update_user_facts(conversation, summary)` is the entry point and never raises. Free-form facts are read via `get_user_facts` / written via `set_user_facts`; profiles via `get_user_profile` / `set_user_profile` — both UPSERT only their own column so the two branches never clobber each other on the shared row. `responder_node` reads both in batch (`get_user_facts_batch` + `get_user_profiles_batch`, fetched via `asyncio.gather`), each cached per user_id for `USER_FACTS_CACHE_TTL = 5 min` in separate caches (`_user_facts_cache` / `_user_profile_cache`; empty results cached too). After writing, `consolidation_node` and `profile_extraction_update_node` write-through their respective cache (`update_user_facts_cache` / `update_user_profile_cache`); the profile node *also reads* that same cache for its `{existing_profiles}` context. Note admin profile writes (`PUT`/`DELETE /user-profile`, bot commands) go straight to the DB **without** write-through, so the profile cache can be up to 5 min stale relative to admin edits. The responder renders each participant as a `User {id}:` block with a `Profile:` section (labelled slots) and a `Notes:` section (free-form facts), injected into the prompt's `{user_facts}`.

**Admin HTTP API** (`app/routers/admin.py`): mirrors the bot commands over REST. Endpoints: `GET/PUT /system-prompt`, `GET /users/names`, `GET /list-chats`, `GET/DELETE /history/{chat_id}` (history items include a `reason` field), `GET/DELETE /summary/{chat_id}`, `GET /personality`, `PATCH /personality/{trait_id}`, `POST /personality/reset`, `GET/PUT/DELETE /user-facts/{user_id}`, `GET /health`.

**Data layer** (`app/repository.py`): all DB functions, each opening its own session via `session_scope`. Both system prompts are cached in module-level globals (`_responder_system_prompt`, `_summarizer_system_prompt`) — setters update both cache and Postgres. `get_history()` joins `History` with `User` to resolve `sender_user_id` into a display name (`username` → `first_name` → stringified ID), and includes each row's `reason`.

**Personality traits**: stored in `personality_traits`. Each row has `low_prompt`/`medium_prompt`/`high_prompt` and `current_value`. Defaults are in `DEFAULT_TRAITS` in `app/prompts.py`. `ensure_traits_seeded()` runs on every startup and **upserts by `name`**: it inserts any trait missing from the table and refreshes `sort_order`/`low_prompt`/`medium_prompt`/`high_prompt` from `DEFAULT_TRAITS` for existing rows (so prompt-text edits in code take effect on restart), but never touches `current_value` (so admin-tuned levels survive). `get_active_trait_prompts()` assembles the active prompt block (one `- Name: active_prompt` line per trait, ordered by `sort_order`) and is injected into `responder_node` as `{personality_traits}`. Its result is cached in module globals (`_active_trait_prompts_cache`/`_active_trait_prompts_cache_time`) for `TRAIT_CACHE_TTL = 5 min` since it's read on every message; `set_trait_value`/`reset_traits`/`ensure_traits_seeded` all call `_invalidate_trait_prompt_cache()` so changes take effect immediately rather than waiting out the TTL.

**Weekly schedule** (`app/models.py::ScheduleActivity`, `app/schedule_data.py`): one row per activity *instance*, keyed by `(day_of_week 0=Mon..6=Sun, start_hour 0-23)` with a unique constraint — `duration_hours` lets a single row span multiple hour-chunks (e.g. `start_hour=23, duration_hours=8` covers 23:00-06:59) rather than duplicating rows per hour. `DEFAULT_SCHEDULE` in `app/schedule_data.py` is the seed data, loaded by `ensure_schedule_seeded()` (insert-if-empty, called from the lifespan alongside the other seeders). Repository queries: `get_current_activity(day_of_week, hour)` (also checks the *previous* day for activities that run past midnight), `get_day_summary(day_of_week)` (name/duration_hours/location only, via `_activity_to_dict(a, partial=True)`).

**Config** (`app/config.py`): pydantic-settings with `@lru_cache`. `DATABASE_URL` must use `localhost:5433` locally (Docker port-mapped) and `db:5432` inside Docker. `WORLDVIEW_PATH` controls where the world-view markdown file lives (default `worldview.md`).

The original Telethon/SQLite implementation is preserved under `Reference/` — `app/repository.py` is a deliberate port of `Reference/app/db.py` with matching function names/signatures, so consult it when porting further pieces.

## Known gaps

- There is a race condition in `_flush_chat`: messages arriving between the flush write and the buffer clear are silently dropped. Marked with a `#TODO` in `client.py`.
