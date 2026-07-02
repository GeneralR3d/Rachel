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

# Start backing services only (Postgres + Neo4j)
docker compose up -d db neo4j

# World-view / Graphiti helper scripts (Neo4j must be up)
uv run python -m scripts.add_worldview_fact "Chagee is a bubble tea brand"  # ingest one fact
uv run python -m scripts.ingest_worldview_md path/to/facts.md               # bulk-ingest a markdown file
uv run python -m scripts.clear_graph                                        # WIPE the entire Neo4j graph (typed confirmation)
```

Copy `template.env` to `.env` and fill in the required values before running. Rachel's own Telegram account credentials are entered interactively during `scripts.login` and saved to `anon.session` — they are never in `.env`.

Runtime dependencies: **Postgres** (chat history, summaries, user facts/profiles, prompts, traits, schedule) **and Neo4j** (Graphiti world-view knowledge graph). Both are defined in `docker-compose.yml`. `DATABASE_URL`/`NEO4J_URI` point at loopback-mapped ports locally and at the compose service hosts (`db`, `neo4j`) inside Docker.

There is no test suite or lint config in this repo.

## Architecture

Rachel is a FastAPI app that runs two Telethon clients on the same asyncio event loop as uvicorn — there is no webhook.

**Startup flow** (`app/main.py`): lifespan seeds both system prompts, personality traits, and the weekly schedule into Postgres, then starts both Telegram clients. Shutdown flushes all in-memory message buffers to DB before disconnecting. `app/main.py` also serves the admin dashboard: `GET /` returns `app/static/index.html` (a self-contained single-file SPA — vanilla JS `fetch`, no build step). It's expected to sit behind nginx basic auth on the VPS, so it has no auth of its own; being same-origin with the API, its `fetch` calls inherit the browser's basic-auth credentials automatically.

**Rachel client** (`app/telegram/client.py`): the core message loop. Per-chat state lives in module-level dicts (`current_messages_buffer`, `wait_tasks`, `flush_tasks`, `last_message_time`). On the first message for a chat, the buffer is seeded from the last `N_PAST_MSG_REQUIRED = 20` DB rows (marked `is_persisted=True`). Each new message cancels and reschedules two asyncio tasks:
- **Reply task** (`wait_tasks`): fires `REPLY_DELAY = 7 s` after the last message. `reply()` slices the last 20 messages from the buffer, computes the distinct `sender_user_ids` in that slice (passed to `get_response` so the responder can load per-user facts), calls `get_response`, sends the response as `\n\n`-separated paragraphs with a simulated typing delay, persists a new summary if the summarizer produced one, and appends the bot's reply to the buffer as `is_persisted=False` along with the responder's `reason`.
- **Flush task** (`flush_tasks`, via `finalize_conversation`): fires `CHAT_BLACKOUT_TIME = 60 s` after the last message. Treats the chat as "conversation finished": snapshots the buffer (plus its summary, from `pending_summaries` or falling back to the DB), calls `_flush_chat` (writes all `is_persisted=False` entries via `add_history_batch`, including each message's `reason`, then clears the chat's buffer — next message re-seeds from DB), and fires off **both** `update_worldview()` and `update_user_facts()` as detached tasks on the snapshotted conversation. Also triggered immediately when the buffer reaches `MAX_BUFFER_LEN = 150` — this cap is now enforced in `new_message` on **every** incoming message (via `finalize_conversation(chat_id, 0)`, so it *does* run both memory pipelines), not in `reply()`, because in group chats Rachel only replies when tagged and a busy untagged group would otherwise reset the flush timer forever and grow the buffer unbounded.

**Reply gating** (`new_message`): Rachel buffers and flushes *every* incoming message, but only schedules a reply when `event.is_private or event.mentioned` — i.e. always in 1-on-1 DMs, and in group chats only when she's @-mentioned or someone replies to one of her messages.

**Admin bot** (`app/telegram/bot.py`): a separate `TelegramClient("bot", ...)` that only responds to `ADMIN_ID`. Commands: `/get_responder_system_prompt`, `/set_responder_system_prompt`, `/get_summarizer_system_prompt`, `/set_summarizer_system_prompt`, `/list_user_names`, `/list_chats`, `/get_history <chat_id>`, `/clear_history <chat_id>`, `/get_summary <chat_id>`, `/delete_summary <chat_id>`, `/list_traits`, `/set_trait <id> <low|medium|high>`, `/reset_traits`, `/get_user_facts <user_id>`, `/set_user_facts <user_id> <facts text>`, `/delete_user_facts <user_id>`. A broad `/set_trait...` fallback handler replies with usage when args don't match `<id> <low|medium|high>`; the specific handler `raise events.StopPropagation` after replying so both don't fire on a valid command.

**LLM service** (`app/services/llm.py`): uses OpenRouter (OpenAI-compatible) via `langchain-openrouter`. A LangGraph graph is compiled once at module import into `_graph` and reused for every call.

The graph is **gated up front, then fans out**: a cheap no-LLM checker decides whether to consult the router; the router decides whether a reply is warranted at all; only then do the summarizer and context_fetcher run **in parallel**, and the responder joins them (LangGraph waits for both before running `responder_node` once):

```
START → checker_node → (must_reply?) → summarizer_node ──↘
              ↓ no                    → context_fetcher_node → responder_node → END
            router_node → (reply needed?) ──↗
              ↓ no
             END
```

- **`checker_node`** / `_route_after_checker`: if the caller set `must_reply` (1-on-1 DM or Rachel was tagged/replied-to), skip the router entirely and fan straight out to `summarizer_node` + `context_fetcher_node` (the conditional edge returns a list of both). Otherwise defer to the router.
- **`router_node`**: LLM gate that decides whether a reply is warranted (uses `ROUTER_PM_SYSTEM_PROMPT` in DMs, `ROUTER_SYSTEM_PROMPT` in groups; only sees the last `ROUTER_CONTEXT_MSGS = 15` messages). Returns `RouterOutput` (`should_reply` + `reason`). Fails **open** (defaults to replying) on LLM error, and salvages `should_reply` from the raw tool-call args when structured parsing drops the `reason` field. If `should_reply` is false the graph short-circuits to END — no summary, no response, no context fetch.
- **`summarizer_node`**: reads `SUMMARIZER_SYSTEM_PROMPT` from DB, injects `{old_summary}` and `{mood_list}`, returns `SummarizerOutput` (mood + summary). If summary is `"NIL"`, only `mood` is written back (leaving `current_summary` unchanged). LLM errors here are swallowed (keeps current mood/summary).
- **`context_fetcher_node`**: the **only** node that uses tool-calling (`_context_fetcher_llm` has `CALENDAR_TOOLS` bound). **Single pass**: one LLM call decides which tools to call, they're all run once, and their concatenated raw output becomes `state["schedule_context"]` (no follow-up round, no summarization turn). It is the sole source of Rachel's schedule knowledge — the responder no longer reads the schedule directly. Its prompt (`CONTEXT_FETCHER_SYSTEM_PROMPT`) mandates fetching "right now" + "today" on every call and any other days/times the conversation references. Wrapped in `asyncio.wait_for(..., CONTEXT_FETCHER_TIMEOUT = 30s)` and fails **open** to empty context on any error/timeout, so it can never stall the pipeline.
- **`responder_node`**: reads `RESPONDER_SYSTEM_PROMPT` from DB and injects, via `ChatPromptTemplate`: `{communication_style}` (tone examples for the *previous* call's detected mood, from `CONVERSATION_STYLE`), `{current_summary}`, `{personality_traits}`, `{conversation_mood}`, `{datetime}`, `{fetched_context}` (from `state["schedule_context"]` — the context_fetcher's schedule output), `{world_view}` (from `await read_worldview(query)` — Graphiti search keyed on the latest human message in the buffer), `{user_facts}` (free-form notes) and `{user_profiles}` (fixed-slot profiles, every slot shown with NIL for gaps) for each distinct `sender_user_id`, and `{profile_attributes}`. Returns `ResponseOutput` (`content` + `reason`, persisted to `History.reason`). Uses `json_mode` instead of tool-calling because tool-calling hangs on the configured model; on a json-parse failure it salvages the raw prose as the reply rather than crashing.

Mood detected by `summarizer_node` is stored in the module-level `_chat_mood: Dict[int, str]` dict and injected into `responder_node` on the **next** call (defaults to `"default"` on first contact) — a deliberate one-call lag.

`get_response()` returns `(response_text, response_reason, new_summary | None, elapsed_seconds)`. `new_summary` is `None` when the summarizer returned NIL (no DB write needed).

**Calendar / schedule tools** (`app/calander.py`): plain async data-access functions (`get_current_activity(day_of_week, hour)`, `get_day_summary`, `get_day_activities`) each open their own `session_scope()` and hit Postgres directly — **no caching** (the old `_current_activity_cache`/`_day_summary_cache` module globals are gone). Wrapping them are the LangChain `@tool`s exposed to `context_fetcher_node`: `get_activity_now()` / `get_today_overview()` (no-arg, use the current SGT time), `get_day_overview(day)` / `get_schedule_for_day(day)` / `get_activity_at(day, hour)` (human day names, resolved via `_resolve_day`). **`CALENDAR_TOOLS` is the single source of truth**: it binds the tools to the LLM *and* drives `format_calendar_tools()`, which renders the tool list (name + arg names + docstring first line) into the `{tools}` placeholder of `CONTEXT_FETCHER_SYSTEM_PROMPT` — so tool descriptions are never hard-coded in `prompts.py`. Adding a tool means adding it to `CALENDAR_TOOLS`, nothing else.

**Mood / tone system** (`app/prompts.py`): `CONVERSATION_STYLE` is a dict keyed by mood name (e.g. `"default"`, `"excited"`, `"sad"`, `"flirt"`). Each value is formatted into `{communication_style}` to steer Rachel's tone. `MOOD_LABELS = list(CONVERSATION_STYLE)` is the single source of truth for valid mood values — the summarizer's structured-output schema is built from it dynamically.

**World view / persistent memory** (`app/services/worldview.py`): a self-contained LangGraph pipeline that runs once per finished conversation (triggered from `finalize_conversation` in `client.py`). Backed by **Graphiti** (a temporal knowledge graph on **Neo4j**) rather than a file — Graphiti does its own entity/edge extraction, de-duplication, and temporal conflict resolution on ingest, so there is no hand-rolled consolidation step. Two nodes run **sequentially**:

```
START → fact_extractor_node → (any new facts?) → ingest_node → END
                                     └── no ──→ END
```

- **`fact_extractor_node`**: reads the just-finished dialogue with `FACT_EXTRACTOR_SYSTEM_PROMPT`, pulls out new durable, general (non-user-specific) facts. Short-circuits to `END` if nothing new.
- **`ingest_node`**: writes each fact into Graphiti as one episode (`add_episode`), sequentially under `_graphiti_lock` (the shared graph races otherwise). Each `add_episode` is several LLM round-trips (node extraction → node resolution/dedup → edge extraction/resolution/invalidation → attribute hydration), which is why ingestion is slow and must stay awaited.

The process-wide Graphiti client is lazily built in `_get_graphiti()` (double-checked under `_graphiti_lock`); its LLM, embedder, and reranker all point at OpenRouter (`graphiti_api_key`, `openrouter_model`/`openrouter_small_model`/`openrouter_embedding_model`). Two Graphiti-specific workarounds live here and are load-bearing:
- The LLM client is `structured_output_mode="json_object"` (not the default `json_schema`) — OpenRouter downgrades `json_schema` to a plain object for models without native constrained decoding, so schema field names aren't enforced and Graphiti's `NodeResolutions`/`ExtractedEdges` fail to validate; json_object mode embeds the schema (field names included) into the prompt instead.
- `_RetryingOpenAIGenericClient` subclasses `OpenAIGenericClient` and re-rolls (with a corrective note) when the model returns valid JSON whose *shape* doesn't match the requested `response_model` — Graphiti validates the dict *after* the call returns, so its built-in tenacity retry (transport errors only) never sees these shape mismatches.

All world-view episodes are written under **`group_id = "worldview"`** (`_WORLDVIEW_GROUP_ID`) and searches are scoped to it, so future per-user memory graphs can use their own group_ids without clashing — all within the single `neo4j` database (an explicit group_id is a no-op for the DB name on the Neo4j driver, which doesn't override `clone`).

`read_worldview(query)` (async, consumed by `responder_node` as `{world_view}`) runs `graphiti.search_(query, config=COMBINED_HYBRID_SEARCH_RRF, group_ids=["worldview"])` and harvests **only edges (relationship facts) and episodes (verbatim ingested sentences)** — node summaries are deliberately dropped because they're a lossy, `MAX_SUMMARY_CHARS=1000`-truncated re-statement of the same edge facts. `update_worldview()` is the entry point called by `client.py` and never raises (errors are caught/logged so memory upkeep can't crash the caller). `settings.worldview_path` is legacy/unused now that storage is Graphiti.

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

**Admin HTTP API** (`app/routers/admin.py`): mirrors the bot commands over REST, mounted at root (no prefix) and consumed by the dashboard. Endpoints: `GET/PUT /responder-system-prompt`, `GET/PUT /summarizer-system-prompt`, `GET /users/names`, `GET /list-chats`, `GET/DELETE /history/{chat_id}` (history items include a `reason` field), `GET/DELETE /summary/{chat_id}`, `GET/PUT/DELETE /user-facts/{user_id}`, `GET/PUT/DELETE /user-profile/{user_id}`, `GET /user-profile-fields` (the fixed profile-slot schema as `[{key,label}]`, derived from `USER_PROFILE_FIELDS` so the dashboard can render every slot — including empty ones — without hardcoding the list), `GET /personality`, `PATCH /personality/{trait_id}`, `POST /personality/reset`, `GET /health`.

**Data layer** (`app/repository.py`): all DB functions, each opening its own session via `session_scope`. Both system prompts are cached in module-level globals (`_responder_system_prompt`, `_summarizer_system_prompt`) — setters update both cache and Postgres. `get_history()` joins `History` with `User` to resolve `sender_user_id` into a display name (`username` → `first_name` → stringified ID), and includes each row's `reason`.

**Personality traits**: stored in `personality_traits`. Each row has `low_prompt`/`medium_prompt`/`high_prompt` and `current_value`. Defaults are in `DEFAULT_TRAITS` in `app/prompts.py`. `ensure_traits_seeded()` runs on every startup and **upserts by `name`**: it inserts any trait missing from the table and refreshes `sort_order`/`low_prompt`/`medium_prompt`/`high_prompt` from `DEFAULT_TRAITS` for existing rows (so prompt-text edits in code take effect on restart), but never touches `current_value` (so admin-tuned levels survive). `get_active_trait_prompts()` assembles the active prompt block (one `- Name: active_prompt` line per trait, ordered by `sort_order`) and is injected into `responder_node` as `{personality_traits}`. Its result is cached in module globals (`_active_trait_prompts_cache`/`_active_trait_prompts_cache_time`) for `TRAIT_CACHE_TTL = 5 min` since it's read on every message; `set_trait_value`/`reset_traits`/`ensure_traits_seeded` all call `_invalidate_trait_prompt_cache()` so changes take effect immediately rather than waiting out the TTL.

**Weekly schedule** (`app/models.py::ScheduleActivity`, `app/schedule_data.py`): one row per activity *instance*, keyed by `(day_of_week 0=Mon..6=Sun, start_hour 0-23)` with a unique constraint — `duration_hours` lets a single row span multiple hour-chunks (e.g. `start_hour=23, duration_hours=8` covers 23:00-06:59) rather than duplicating rows per hour. `DEFAULT_SCHEDULE` in `app/schedule_data.py` is the seed data, loaded by `ensure_schedule_seeded()` (insert-if-empty, called from the lifespan alongside the other seeders). Query functions live in `app/calander.py` (not `repository.py`): `get_current_activity(day_of_week, hour)` (also checks the *previous* day for activities that run past midnight), `get_day_summary(day_of_week)` (name/duration_hours/location only, via `_activity_to_dict(a, partial=True)`), and `get_day_activities(day_of_week)` (full detail). See the **Calendar / schedule tools** subsection above for how these are surfaced to the LLM.

**Config** (`app/config.py`): pydantic-settings with `@lru_cache`. `DATABASE_URL` must match the environment (loopback-mapped port locally vs `db` service host in Docker); same for `NEO4J_URI` (`bolt://localhost:7687` locally vs the `neo4j` service host). OpenRouter drives everything: `openrouter_api_key` for Rachel's own router/summarizer/responder, and a **separate** `openrouter_graphiti_api_key` (exposed via the `graphiti_api_key` property, falling back to the main key when unset) billed for all Graphiti/world-view LLM+embedder+reranker calls. `WORLDVIEW_PATH` is a legacy setting — world-view storage is now Graphiti/Neo4j, not a markdown file.

The original Telethon/SQLite implementation is preserved under `Reference/` — `app/repository.py` is a deliberate port of `Reference/app/db.py` with matching function names/signatures, so consult it when porting further pieces.

## Known gaps

- There is a race condition in `_flush_chat`: messages arriving between the flush write and the buffer clear are silently dropped. Marked with a `#TODO` in `client.py`.
