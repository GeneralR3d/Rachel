# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

Keep this repository guidance synchronized between `AGENTS.md` and `CLAUDE.md`; only the title and agent introduction should differ.

## Setup and commands

Python **3.11+** and `uv` are required. Run commands from the repository root.

```bash
uv sync
cp template.env .env                 # first setup only; fill in credentials

docker compose up -d db neo4j        # backing stores for local development
uv run python -m scripts.login      # interactive, one-time anon.session setup
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Postgres stores history, chat state, structured user profiles, traits, weekly activities, model selections, and proactive schedules. Neo4j stores Graphiti world-view and per-user fact episodes. Compose maps Postgres to **localhost:5433** and Neo4j Bolt to **localhost:7687**. Inside Compose, use `db:5432` and `neo4j:7687`; `docker-compose.yml` supplies these overrides for the app service. The bare default in `app/config.py` uses Postgres port 5432, so use the local URL from `template.env` for the Compose-backed database.

`anon.session` and `bot.session` are Telethon session files. Login establishes Rachel's session interactively; the application lifespan starts both clients with the configured `TELEGRAM_BOT_TOKEN`. `.env`, session files, and local Phoenix credentials are ignored by Git.

```bash
# Migrations and lightweight syntax checks
uv run alembic revision -m "describe schema change"  # blank revision; edit explicitly
uv run alembic downgrade -1
uv run alembic heads
uv run python -m compileall -q app scripts alembic

# Python regression tests and zero-build dashboard tests
uv run python -m unittest discover -s tests
node --test tests/test_proactive_dashboard.cjs

# Graphiti helpers; require Neo4j and configured Gateway credentials
uv run python -m scripts.add_worldview_fact "Chagee is a bubble tea brand"
uv run python -m scripts.ingest_worldview_md path/to/facts.md
uv run python -m scripts.ingest_user_facts_md 12345  # reads user_fact_12345.md

# Workflow PNGs (see the missing Mermaid source under Known limitations)
uv run python -m scripts.draw_graphs
```

Tests use standard-library `unittest` with mocks at model/Telegram boundaries. Imports need valid app settings; placeholder API credentials suffice for unit tests. Set `TEST_DATABASE_URL` to a **disposable Postgres database** to include database integration tests; otherwise those tests skip. They create and remove isolated schemas. Node's built-in test runner exercises dashboard JavaScript without a browser. There is no lint configuration.

`scripts.clear_graph` wipes **every** Neo4j partition, including all user facts. It requires the exact typed confirmation `delete everything` unless `--yes` is supplied. Bulk ingestion is additive: Graphiti resolves duplicate entities/edges, but re-running an import creates fresh raw episodes.

## Application lifecycle and layout

Rachel is a FastAPI application with two Telethon clients and an in-process APScheduler on the **same asyncio event loop**. There is no Telegram webhook.

- `app/main.py`: registers Phoenix instrumentation, seeds traits, weekly activities, and model roles, starts both Telegram clients, upserts Rachel's user record, then starts proactive scheduling. Shutdown stops and drains the scheduler, flushes buffers, disconnects the clients, and disposes the SQLAlchemy engine, in that order.
- `app/telegram/client.py`: incoming messages, reply timing, speech controls, buffers, memory finalization, and the shared reactive/proactive send path.
- `app/telegram/bot.py`: separate `TelegramClient("bot", ...)` restricted to `ADMIN_ID`.
- `app/services/llm.py`: compiled reactive and proactive LangGraph workflows, lazy model clients, router decisions, retrieval passes, and responder validation.
- `app/prompts.py`: source-controlled system prompts, tool guidance, persona, moods, trait defaults, and profile schema. Prompts are no longer stored or edited in Postgres.
- `app/services/prompt_builders.py` and `app/services/context.py`: pure prompt/context assembly and typed raw tool observations.
- `app/services/proactive.py`, `app/services/proactive_schemas.py`, and `app/services/proactive_tools.py`: scheduler lifecycle, shared validation, and chat-scoped schedule tools.
- `app/services/memory.py`, `app/services/worldview.py`, `app/services/userfacts.py`, and `app/services/graphiti.py`: conversation extraction and persistent memory.
- `app/repository.py`, `app/models.py`, and `app/database.py`: data access and ORM models. `session_scope()` commits on success and rolls back on error; repository operations normally open their own session.
- `app/calander.py` (existing spelling) and `app/schedule_data.py`: weekly activity queries, calendar tools, and seed data.
- `app/routers/admin.py` and `app/static/index.html`: root-mounted REST API and a self-contained vanilla-JavaScript dashboard, with no build step.

`GET /` serves the dashboard and `/metrics` mounts the Prometheus ASGI application. The HTTP API and dashboard have no application authentication; deployment expects nginx basic auth or an equivalent access layer, including for metrics. The optional Compose `phoenix` service exposes its UI on localhost:6006 and receives app traces through the configured Phoenix endpoints.

## Telegram message lifecycle

Per-chat state is process-local: buffers, `burst_sessions`, `pending_summaries`, reply/flush tasks, reply/buffer locks, `responded_watermark`, `pending_mention`, and mute/silence state.

- On first use, seed the buffer from the latest **40** persisted history rows. `_append_to_buffer`, `_snapshot_context`, `_flush_chat`, and bot-message insertion share `buffer_locks[chat_id]` so initialization, snapshotting, persistence, and buffer removal cannot drop concurrent appends. Do not hold this lock during LLM generation.
- Usable incoming messages are buffered even when Rachel stays quiet. Non-text media gets a placeholder such as `[photo]` or a sticker emoji; genuinely empty events are skipped. Sender records are upserted, with first names preferred for dialogue labels.
- Every unsuppressed private or group message schedules a reply **7 seconds** after the latest message. A direct mention/reply latches `pending_mention` across the whole burst and forces admission past the router. Otherwise a **5%** spontaneity draw can force admission; ordinary DMs and untagged groups use their respective router questions. DMs are not unconditionally forced replies.
- `reply_locks[chat_id]` serializes generation, sending, and bot-message insertion for reactive and proactive messages. Once the debounce expires, `asyncio.shield(reply(...))` lets the committed reply finish despite later timer cancellation. `_generate_response_with_retry` retries a failed graph invocation once within the same trace.
- `_reply` snapshots the latest 40 messages and participant ID-to-name map. It passes the previous `responded_watermark` to the graph, then advances the in-memory watermark to the newest snapshot ID **before generation**. A no-new-user-message guard overrides even a forced reply; router-suppressed turns have still been considered.
- Responses split on `\n\n` into Telegram bursts; later paragraphs simulate typing at **22 characters/second**. The complete reply and its `reason` are inserted by the first sent message ID, preserving ID order. If a concurrent flush already removed the buffer, `_append_or_persist_bot_message` writes the reply directly to Postgres.
- Summary changes remain in `pending_summaries` until flush. `_load_summary_and_mood` restores persisted summary/mood when needed and normalizes an absent summary to `""`.
- After **180 seconds (3 minutes)** of inactivity, `finalize_conversation` atomically snapshots and flushes the buffer and pending summary/mood, detaches `update_memories`, and advances the durable memory watermark to the newest snapshot ID. Reaching **150** buffered messages schedules immediate finalization on every incoming event, including quiet groups. Shutdown calls `_flush_chat` directly and does not start memory extraction.

Strict, case-insensitive group controls use `BOT_NAME` (normally Rachel) followed by an action. `Rachel shush`, `Rachel shut up`, `Rachel quiet`, `Rachel silence`, or `Rachel stop talking` arms a **15-minute** silence; `Rachel mute` mutes indefinitely. `Rachel speak` or a direct mention/reply clears both modes. Muted/silenced chats keep buffering and flushing while skipping reply scheduling. Confirmations wait behind the reply lock; mute confirmations check a transition token to suppress stale confirmations. Speech suppression is not persisted across restarts.

## Reactive reply graph and retrieval loop

`_graph` is compiled once at import. With new user content, the checker either forces admission or consults the router. Admission runs summarization and the initial context pass in parallel; the responder joins them. Later retrieval passes loop only through context fetcher and responder:

```text
START -> checker -> router (unless forced) -> admission
            |             | no reply -> END
            | no new -> END
admission -> summarizer -----------------> responder -> END
          -> context_fetcher ------------> responder
                    ^                         |
                    +------ fetch_more -------+
```

- **Router:** direct `httpx` POST to `merge_gateway_base_url + "/decisions"`, with a 15-second timeout and model `DECISIONS_LLM` (default `typesafe/jev-1.13`). It sees the summary and latest **15** messages, chooses `ROUTER_PRIVATE_QUESTION` or `ROUTER_GROUP_QUESTION`, validates a finite `noul` probability in `[0, 1]`, and replies at **>= 0.7**. Errors or invalid answers fail open. Rejection skips summarization, retrieval, and response generation.
- **Summarizer:** uses `SUMMARIZER_SYSTEM_PROMPT`, the entire undivided history, and a dynamic mood enum from `CONVERSATION_STYLE`. Its output schema requests a summary of at most 200 words, or `NIL` to preserve the summary, plus mood. Failures preserve existing state. The responder reads the joined graph state's mood, including a successful current-call update; `get_response` also saves it in `_chat_mood` for later calls. Older comments claiming a deliberate one-call mood lag do not match this implementation.
- **Context fetcher:** owns all tool execution. `CONTEXT_TOOLS` combines `CALENDAR_TOOLS`, `search_world_view`, the name-based `search_user_info_tool`, and `PROACTIVE_TOOLS`. Each pass makes one tool-selection call and executes its requested tools sequentially under a shared **30-second** deadline. Completed results survive a later timeout.
- **Observations:** `GraphState.tool_observations` accumulates `ToolObservation` records with pass number, tool name, arguments, category, status (`success`, `empty`, `error`, `timeout`), and raw result. Model failures, unknown tools, ambiguous participant names, and individual tool failures become explicit observations. Do not collapse failed retrieval into successful empty facts or replace observations with an LLM summary.
- **Responder:** sees summary, traits, current mood/style, SGT date/time, history, and rendered observations. `ResponseOutput` requires `decision` (`respond` or `fetch_more`), `retrieval_request`, `content`, and `reason`, and forbids extra keys. It may request another lookup or supported schedule action; only the fetcher selects and executes tools. Even `fetch_more` must carry a sendable best-effort reply.
- **Bounds/fallback:** `AGENT_MAX_CONTEXT_PASSES` defaults to **3**, accepts **1–10**, and includes the initial pass. Another pass requires a non-empty request, unused pass budget, and more than two remaining graph steps. Summarization is not repeated. The runner sets recursion limit `2 * passes + 8`; emergency recovery preserves an existing reply or supplies a clarification. Malformed responder output salvages visible `content` or prose without exposing serialized decision/handoff fields, then ends the loop. Outcomes are `first_pass`, `retried`, or `capped`.

`get_response(...)` returns `(response_text, response_reason, new_summary_or_none, elapsed_seconds)`. Router rejection returns empty response text. The `reason` is persisted in `History.reason`.

**History partitioning:** `_build_history_messages` emits concrete `AIMessage`/`HumanMessage` objects so literal braces in dialogue are not template placeholders. With a reply watermark, all Rachel turns and handled user messages are context above the divider; only non-bot messages with a greater Telegram ID appear below it. Relative order within each partition is preserved. Without a watermark, fall back to one past Rachel's last turn. Summarization receives no divider. This reply watermark is separate from the persistent extraction watermark.

## Prompts, tools, and model clients

`RESPONDER_SYSTEM_PROMPT`, `SUMMARIZER_SYSTEM_PROMPT`, and extractor prompts come directly from `app/prompts.py`. Responder/summarizer HTTP endpoints and bot commands expose them read-only; prompt setters and the `system_prompts` ORM/repository paths were removed.

`build_context_fetcher_prompt` derives tools from the actual tool objects and their complete docstrings, adding only matching `CONTEXT_FETCHER_TOOL_GUIDANCE`. `context_fetcher_dynamic_prompt` supplies participants, SGT time, and optional outreach intent through LangChain middleware. `build_responder_context` uses `RESPONDER_TOOL_RESULT_SECTIONS`; failures stay in retrieval status, and looked-up empty profiles show unknown slots as `NIL`. Prompt, handoff, and loop messages are assembled separately so runtime data is not re-templated.

The LLM-facing personal-memory tool is **`search_user_info(name, query)`**. The fetcher resolves normalized participant names to real user IDs and rejects missing or ambiguous matches. It calls `userfacts.search_user_info(user_id, query)` to obtain Graphiti facts and the structured profile together. The responder does not independently load schedules or participant memory.

Merge Gateway has two distinct base paths:

- `MERGE_GATEWAY_OPENAI_BASE_URL` (default `https://api-gateway.merge.dev/v1/openai`) for chat completions.
- `MERGE_GATEWAY_BASE_URL` (default `https://api-gateway.merge.dev/v1`) for embeddings and router decisions.

Reply clients and the three memory extractors use `MERGE_GATEWAY_API_KEY`. Graphiti's internal LLM/embedder/reranker use `MERGE_GATEWAY_GRAPHITI_API_KEY`, falling back to the main key through `settings.graphiti_api_key`.

Client settings differ deliberately: summarizer and memory extractors use `method="function_calling"` with `reasoning_effort="none"`; the context fetcher binds real tools with **medium** reasoning; the responder uses **medium** reasoning and strict `method="json_schema"`, `include_raw=True`, followed by local Pydantic validation. Preserve these distinctions and validate model-route capabilities when switching models. Persisted Telegram history contains visible reply text only.

`llm_models` is the catalog and `active_models` selects **main**, **small**, and **embedding** roles. Startup seeds configured models and missing active rows without overwriting admin selections. Lazy reply/extractor clients are cached by active main model. `set_active_model` invalidates the active-model cache, reply/extractor clients, and Graphiti singleton so subsequent calls rebuild without a restart. Model IDs use `provider/model`; config accepts legacy `OPENROUTER_MODEL`, `OPENROUTER_SMALL_MODEL`, and `OPENROUTER_EMBEDDING_MODEL` aliases. The JEV router model remains separately controlled by `DECISIONS_LLM`.

## Proactive outreach

`ProactiveSchedule` persists name, Telegram chat ID, five-field cron, IANA timezone (default `Asia/Singapore`), outreach intent, enabled state, repeat limit/counter, timestamps, and last status/error. `validated_schedule_data` trims fields, validates cron/timezone, and verifies Telegram access; HTTP handlers and agent tools share it.

`start_proactive_scheduler` rebuilds enabled, non-exhausted jobs from Postgres using `AsyncIOScheduler`. Jobs use `max_instances=1`, coalescing, and 60-second misfire grace. `run_schedule` also uses a process-local per-schedule lock. `claim_proactive_schedule_run` atomically increments `repeat_completed`, records `running`, and commits **before** generation or sending. Attempts, including failed and manual runs, consume allowance. `repeat_times=null` means unlimited; a positive integer is the total lifetime allowance, and `1` provides one attempt. Updates preserve the consumed counter; enabling an exhausted schedule does not reset it. Manual runs may bypass `enabled` but still enforce the limit.

`send_proactive_message` waits for delayed reactive replies and timed silence, acquires the shared reply lock, and rechecks conditions. Indefinite mute fails the attempt. It loads summary/history through the normal lifecycle, generates outreach, sends paragraph bursts, inserts or directly persists the bot message, and resets the inactivity timer.

`_proactive_graph` runs `context_fetcher -> responder`, with the same bounded retrieval loop but **no checker, router, summarizer, or mood writes**. `get_proactive_response` returns `(text, reason, elapsed_seconds)`. The intent is a new system-level trigger; every history item is prior context. A fired reminder must deliver its intent now, rather than answer the old setup request or recreate its schedule.

`PROACTIVE_TOOLS` exposes create, list, update, enable/disable, and delete operations to the fetcher. Tools obtain `current_chat_id` from application-supplied `RunnableConfig`, never model arguments, and scope reads/writes to that chat. Preserve these ownership checks. There is no agent-facing run-now tool. Tool create/update explicitly requires `repeat_times` (positive integer up to 99999 or null); HTTP permits positive Postgres bigint limits. Updates replace editable details and enable the job; toggling preserves them.

Agent schedule writes use `apply_schedule_mutation` so persistence and live job synchronization finish in a strongly referenced shielded task even if the context pass times out. Shutdown pauses future occurrences and drains active executions and mutations before Telegram disconnects.

## Memory pipelines and Graphiti

`update_memories(conversation, summary, chat_id, last_processed_id, ...)` divides a finished snapshot **once** using `build_extraction_history`, then concurrently runs world-view and user-memory workflows. The full transcript stays available, with `EXTRACTOR_DIVIDER` before messages newer than `chat_state.last_processed_message_id`. With `has_new=False`, extractors skip model calls. `finalize_conversation` advances this watermark when handing off the snapshot, not after successful extraction.

- **World view:** `fact_extractor_node -> ingest_node`, or END with no new general facts. Each fact becomes an episode under `group_id="worldview"`.
- **Personal facts:** an independent extract/ingest branch writes episodes under `user_facts_group_id(user_id)`, yielding `user-facts-<id>`. Models identify people by sender name; `update_user_facts` builds the name-to-ID map from the entire raw conversation, excluding Rachel, and drops unrecognized names.
- **Profiles:** `profile_extraction_update_node` runs in parallel with personal fact extraction and writes JSONB in `user_facts_preferences`. Cached existing profiles provide prompt context, but authoritative profiles are read from DB under per-user `_user_locks`. The merge keeps known keys and overwrites only newly extracted non-empty values, then writes through the profile cache. There is no LLM consolidation step.

`USER_PROFILE_FIELDS` in `app/prompts.py` is the schema source for dynamic Pydantic models, guidance, rendering, and the admin field list. Slot changes need no migration. The profile cache in `llm.py` batches misses and caches empty profiles for **5 minutes**. Admin profile writes/deletes bypass write-through and can remain stale for that TTL. Lazy imports from `userfacts` back to `llm` avoid a circular import. Logs distinguish `[userfacts]` and `[userprofile]`.

`app/services/graphiti.py` owns a lazy, double-checked singleton, `_graphiti_lock`, ingestion, search, and episode listing. Ingestion is sequential and awaited under the lock; one episode involves several model round-trips, and Graphiti handles entity deduplication and temporal edge conflicts. Both memory entry points catch/log failures so they cannot crash reply handling.

`search_graph` uses `COMBINED_HYBRID_SEARCH_RRF` scoped to a group ID and returns deduplicated **edge facts plus verbatim episodes**, excluding node summaries. Default search errors return empty text; the reply loop passes `raise_on_error=True` to produce failure observations. `list_episodes` returns all episode bodies oldest first for admin inspection.

Preserve Graphiti's compatibility workarounds:

- `_RetryingOpenAIGenericClient` uses `structured_output_mode="json_object"` to embed schema guidance, but `_build_response_format` returns OpenAI's `NOT_GIVEN` so HTTP requests omit `response_format`.
- Malformed JSON and schema failures share **three corrective retries** after the first attempt. On exhaustion, malformed JSON raises `ValueError` so outer transport retries cannot restart the format budget; schema-invalid dictionaries are counted and passed through for Graphiti validation. Only terminal failures are recorded.
- The configured OpenAI reranker expects log probabilities the Gateway does not supply. Current RRF search does not invoke it; switching to a cross-encoder reranker requires addressing that incompatibility.

## Data, traits, and weekly activities

`History` uses the composite primary key **`(chat_id, telegram_message_id)`** because Telegram IDs are only unique within a chat. Batch inserts validate parallel list lengths and ignore duplicate conflicts. `get_history` returns ascending ID order, resolving names as first name, username, then numeric ID, and includes `reason`. `created_at` is timezone-aware and server-defaulted.

`ChatState` holds summary, mood, and the durable extraction watermark. Summary/mood and watermark upserts update their own fields; the empty summary default permits a watermark-first row for chats Rachel has not answered.

`ensure_traits_seeded` upserts `DEFAULT_TRAITS` by name, refreshing order and prompt text while preserving admin-selected levels. `get_active_trait_prompts` caches the rendered block for **5 minutes**; seed/set/reset invalidate it immediately.

`ScheduleActivity` represents one weekly instance, unique on `(day_of_week, start_hour)`, with day 0=Monday and duration spanning hours or midnight. `ensure_schedule_seeded` inserts `DEFAULT_SCHEDULE` only into an empty table. Queries in `app/calander.py` open a session per call without caching; current activity also checks the previous day for overnight events. `CALENDAR_TOOLS` exposes current/today, day overview/detail, and day/hour queries using SGT and resolved day names. Weekly persona activities and proactive cron schedules are separate stores.

Alembic's current head is `0a7e2c9d4b61`, after `f9c6a1d4b3e8` (drops `system_prompts`) and `e8b5d0f3a2c7` (repeat limits). Preserve deployed revision IDs and ancestry. The timestamp migration uses `ADD COLUMN IF NOT EXISTS` so existing timestamps survive. Downgrading prompt-table removal recreates an empty schema and cannot recover deleted prompts.

## Admin surfaces

The HTTP API is root-mounted; `app/routers/admin.py` is the endpoint/schema source of truth.

- Read-only prompts: `GET /responder-system-prompt`, `/summarizer-system-prompt`, `/workflow-prompts`.
- Users/chats: `GET /users/names`, `/list-chats`; chat names resolve concurrently through Telegram, with failures returning null names.
- History/summary: `GET` and `DELETE /history/{chat_id}` and `/summary/{chat_id}`.
- Profiles: `GET`, `PUT`, `DELETE /user-profile/{user_id}`; `GET /user-profile-fields` derives labels from the shared schema.
- Facts: `GET`, `POST /user-facts/{user_id}` and `/worldview-facts`. Ingestion is slow; there is no episode edit/delete API, and Graphiti resolves superseding facts during ingestion.
- Traits: `GET /personality`, `PATCH /personality/{trait_id}`, `POST /personality/reset`.
- Models: `GET`, `POST`, `DELETE /models` (delete uses a `model_string` query parameter); `GET /active-models`, `PUT /active-models/{role}`.
- Outreach: `GET`, `POST /proactive-schedules`; `PUT`, `DELETE /proactive-schedules/{schedule_id}`; `PATCH /proactive-schedules/{schedule_id}/enabled`; `POST /proactive-schedules/{schedule_id}/run`. Responses include consumed attempts, last status/error, and next run time; exhausted jobs have no next run.
- Diagnostics: `GET /health` is a simple status response; `GET /architecture` renders diagrams off the event loop and returns base64 PNGs, surfacing rendering failures as HTTP 502.

The admin bot supports prompt getters, user/chat inspection, history/summary get/delete, user-fact add/list, profile get/delete, trait list/set/reset, and model management (`/list_models`, `/list_active_models`, `/add_model`, `/delete_model`, `/set_active_model`). It has no prompt setters. Specific handlers with broad usage fallbacks raise `events.StopPropagation` after handling valid commands. Rachel's client also implements private-chat `/clear_history` and `/update_history` handlers.

## Observability

`app/services/metrics.py` defines `rachel_llm_calls_total{node}` and `rachel_llm_errors_total{node,kind}` for router, summarizer, context fetcher, responder, world-view extractor, user-fact extractor, profile extractor, and internal Graphiti LLM calls. Attempts count at invocation; errors count at terminal failure/skip boundaries. `record_llm_empty_output` covers missing parsed extractor results. Error kinds distinguish timeout, output parsing, HTTP status (including codes embedded in validation errors), schema validation, and other failures. Recovered Graphiti retries and cooperative responder cancellations do not count as errors. Graphiti embeddings/reranking and non-LLM infrastructure failures are outside these counters.

`rachel_agent_loop_outcomes_total{outcome}` and the `rachel_agent_context_passes` histogram cover admitted reactive/proactive loops. Phoenix workflow spans (`telegram_reply`, `telegram_proactive`, `memory_update`) are separate roots, grouped with a `burst_sessions` session ID when available and Telegram chat metadata. The direct JEV call has a manual LLM span including validated probability, model/provider, and available usage/cost.

Direct world-view and user-memory lookups use `traced_tool_call` in `app/services/tracing.py` to create `TOOL` spans under the active context-fetcher span in both reply graphs. Spans preserve session/chat metadata and record tool name/call ID, arguments, retrieval pass/category/status, and complete results (text or JSON, including personal facts and the structured profile). Empty results, errors, and deadline cancellations retain their observation status/output; errors and timeouts mark the tool span as failed without changing reply handling. Calendar, proactive-schedule, and URL tools keep their automatic LangChain spans to avoid duplicates.

## Known limitations

- Run one app process: Telethon sessions, timers, caches, locks, and APScheduler are process-local; Prometheus uses the default single-process registry. Multiple workers would duplicate scheduling and require coordination.
- Multi-paragraph replies persist as one row under the first paragraph ID, so interleaved messages cannot be represented at per-paragraph granularity.
- Reply watermarks and speech suppression are process-local. Extraction watermarks persist but advance at handoff; failed extraction is not automatically replayed.
- Changing embedding dimensionality does not re-embed or rebuild existing Neo4j data automatically.
- `scripts/draw_graphs.py` registers four workflows, but its hand-arranged reactive diagram source `scripts/graph_llm.mmd` is currently absent from this checkout. CLI rendering and `/architecture` fail until it is restored; existing root PNGs may be stale.
- `WORLDVIEW_PATH` is legacy configuration; facts live in Neo4j. `Reference/` is an ignored local copy of the original implementation and may be absent in a fresh clone.

Treat code as authoritative when older comments, README sections, deployment guides, or design specs disagree. Update both instruction files together when architecture or development commands change.
