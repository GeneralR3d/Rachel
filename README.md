# Rachel

> A stateful Telegram persona for private and group chats that remembers people, reads the room, and follows up on scheduled reminders.

![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Neo4j](https://img.shields.io/badge/Neo4j-Graphiti-4581C3?logo=neo4j&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-stateful%20workflows-F97316)
![Merge Gateway](https://img.shields.io/badge/LLM-Merge%20Gateway-111827)

## What is Rachel?

Most chatbots forget who they are talking to, answer every message immediately, and become noisy the moment they enter a group chat. Rachel is designed around the opposite goal: feel like a believable participant who knows when to speak, remembers what matters, and carries context across conversations.

The shipped persona is Rachel, a 22-year-old NTU marketing student in Singapore with a backstory, seven conversational moods, twelve tunable personality traits, and a weekly schedule. She waits for a burst of messages to finish, types at a human pace, can stay quiet when a conversation does not concern her, and recalls both general world knowledge and personal facts about the people she meets.

Underneath the character is an asynchronous persona engine: FastAPI hosts two Telethon clients and an in-process scheduler, LangGraph coordinates reactive replies, proactive outreach, and memory workflows, PostgreSQL stores operational state, and Graphiti on Neo4j provides temporal long-term memory. Model and embedding traffic goes through Merge Gateway. Persona and workflow prompts live in source control; traits, weekly activities, memories, and active model selections have persistent stores.

## Key Innovations

### 1. Human cadence with race-safe delivery

Rachel does not answer directly inside Telegram's message-arrival event. Each unsuppressed incoming turn resets a seven-second reply debounce; a separate three-minute inactivity timer controls conversation finalization. Multi-paragraph answers are emitted as separate Telegram messages, with later paragraphs simulating typing at 22 characters per second. Per-chat locks, shielded in-flight replies, separate watermarks, and message-ID-ordered buffer insertion protect delivery when new messages arrive mid-generation.

### 2. Bounded retrieval with explicit failure observations

After admission, summarization and the initial context pass run in parallel. The responder evaluates the collected evidence and can request another pass, while only the context fetcher selects and executes tools. Raw results retain their tool, arguments, pass number, and `success`, `empty`, `error`, or `timeout` status, so a failed lookup cannot silently become a claim that no facts exist. A configurable pass budget and a 30-second deadline per pass bound retrieval; completed results survive a later timeout, and every responder pass prepares a sendable fallback.

### 3. Separate boundaries for replies and memory extraction

Reply generation and memory extraction track “already handled” independently. An in-memory reply watermark keeps new user messages visible even when Rachel's later send-time message ID sorts after them; a persisted extraction watermark divides finished transcripts into old context and new material. Advancing extraction state at handoff avoids routine re-extraction after a flush or restart, with a deliberate tradeoff: failed background extraction is not automatically replayed.

### 4. Temporal graph facts and deterministic profile merges

General facts and personal memories become Graphiti episodes in separate partitions. Hybrid retrieval uses `COMBINED_HYBRID_SEARCH_RRF`, returning deduplicated relationship facts and verbatim episodes while excluding lossy node summaries. Graphiti resolves entities and temporal conflicts; the 16 fixed profile slots instead merge deterministically in PostgreSQL JSONB under per-user locks, where new non-empty values replace older values. One source schema drives extraction, rendering, and the admin field list without a database migration for each slot change.

### 5. Durable outreach allowances with a shared delivery path

Proactive schedules are stored in PostgreSQL and rebuilt into APScheduler jobs on startup. A conditional database update consumes each attempt before generation or sending, enforcing lifetime limits even when an attempt fails. Reactive and proactive messages share the same per-chat reply lock, typing simulation, history insertion, and silence controls; agent schedule tools receive their chat scope from runtime configuration. Shielded mutations finish both persistence and live job synchronization even if the requesting context pass times out.

### 6. Public URL reading with network and prompt boundaries

The context fetcher can read relevant public HTTP(S) links without a browser or search service. Each redirect destination is validated, DNS results must all be public, and the connection is pinned to a checked address while retaining the original HTTP/TLS host identity. Fetches have explicit time, size, redirect, and per-reply attempt limits. Extracted content and URL errors enter prompts as explicitly untrusted data in concrete human messages, keeping page text out of system instructions.

### 7. Runtime model control plus gateway-specific reliability

The main, small, and embedding model roles are stored in PostgreSQL and can be switched from Telegram, REST, or the dashboard. Cache invalidation rebuilds model clients and the Graphiti singleton on subsequent calls. The responder uses strict JSON Schema output with local validation and visible-text recovery; Graphiti embeds schema guidance in its prompt while omitting the HTTP `response_format` field for route compatibility. Malformed JSON and schema mismatches share three corrective retries after the first attempt, preventing transport retries from multiplying an exhausted format budget.

### 8. Failure-aware observability that matches product behavior

Prometheus records model calls, terminal errors, retrieval pass counts, and loop outcomes with bounded labels. Phoenix traces reactive replies, proactive outreach, and memory updates as separate roots connected by burst-session and chat metadata. Direct memory lookups receive explicit tool spans with arguments, complete results, and retrieval status; the direct JEV router call records its validated probability and available usage/cost. This connects a visible reply to its routing and retrieval evidence without treating recovered Graphiti retries as terminal failures.

## Architecture

```mermaid
flowchart TB
    subgraph TG[Telegram]
        U[People and group chats]
        A[Administrator]
    end

    subgraph APP[FastAPI · one asyncio event loop]
        RC[Rachel Telethon client]
        BOT[Admin Telethon bot]
        API[REST API · dashboard · metrics]
        STATE[Per-chat buffers · locks · scheduler]
    end

    subgraph AI[LangGraph workflows]
        REPLY[Reactive and proactive reply graphs]
        MEM[World-view and user-memory extraction]
    end

    GW[Merge Gateway<br/>chat · decisions · embeddings]
    PG[(PostgreSQL<br/>history · profiles · schedules · models)]
    N4J[(Neo4j + Graphiti<br/>temporal memories)]

    U -->|Telegram events| RC
    RC -->|incoming messages| STATE
    STATE -->|debounced turns or outreach intent| REPLY
    REPLY -->|typed message bursts| U
    STATE -->|finalized transcript| MEM
    A <-->|admin commands| BOT
    A <-->|dashboard and REST| API
    REPLY <-->|traits · profiles · schedule tools| PG
    STATE <-->|history · summaries · saved jobs| PG
    MEM -->|profiles · watermark| PG
    REPLY <-->|hybrid memory search| N4J
    MEM -->|fact episodes| N4J
    REPLY <-->|model calls| GW
    MEM <-->|model and embedding calls| GW
    BOT <-->|configuration| PG
    API <-->|configuration and history| PG
```

- **Telegram client** (`app/telegram/client.py`) owns message normalization, per-chat buffers, reply/flush timers, group silence controls, typing simulation, and graceful persistence.
- **Reply workflow** (`app/services/llm.py`) gates participation, summarizes the conversation, retrieves only relevant context, and generates a structured response plus a traceable reason.
- **Proactive scheduler** (`app/services/proactive.py`) restores cron jobs, claims attempts, coordinates live schedule changes, and initiates outreach through the shared Telegram delivery path.
- **Memory workflows** (`app/services/memory.py`, `worldview.py`, `userfacts.py`) partition finalized transcripts once, then update general knowledge, personal facts, and structured profiles concurrently.
- **Graphiti adapter** (`app/services/graphiti.py`) initializes Neo4j indices, serializes graph writes, performs hybrid retrieval, and adds schema-aware retry behavior around Graphiti's LLM client.
- **Data layer** (`app/models.py`, `app/repository.py`, `app/database.py`) uses async SQLAlchemy sessions and PostgreSQL upserts for durable chat and control-plane state.
- **Admin plane** (`app/telegram/bot.py`, `app/routers/admin.py`, `app/static/index.html`) exposes Telegram commands, REST endpoints, a dashboard without a build step, and Prometheus metrics. Architecture rendering currently has a missing-source limitation described below.

### Proactive outreach repeat limits

Create schedules in the dashboard's **Proactive** tab or through the chat agent's schedule tools. Each schedule includes a reachable Telegram chat, an outreach intent, a five-field cron expression, and an IANA timezone (default `Asia/Singapore`). For example, `0 9 * * *` fires daily at 09:00 in that timezone; the persona's weekly activity calendar is a separate store.

Each proactive schedule has `repeat_times` (a positive integer, or `null` for unlimited) and a read-only `repeat_completed` counter. The dashboard accepts a blank repeat limit for unlimited schedules and displays consumed attempts. PostgreSQL atomically checks the limit and commits the increment before generation or sending starts; failures, interruptions, and process crashes still consume that attempt. “Run now” can execute a disabled schedule, but shares the same allowance as cron fires.

Exhausted schedules show **Done** and are removed from the active scheduler without changing their saved `enabled` setting. Raising the limit above the consumed count, or clearing it to unlimited, resumes an enabled schedule. Editing or toggling a schedule never resets its counter. Existing schedules migrate to unlimited with zero counted attempts; older executions are not reconstructed. Apply `uv run alembic upgrade head` before starting the updated application.

Agent tools can create, list, replace, enable/disable, and delete schedules only in the current chat; there is no agent-facing run-now tool. Agent create/update calls require an explicit repeat limit of `1–99999` or `null`; HTTP accepts positive PostgreSQL bigint limits. Replacing a schedule through the agent enables it and preserves consumed attempts. Disabling or deleting removes future occurrences but does not cancel an execution already underway.

The proactive graph runs context fetcher and responder with the same retrieval bounds as reactive replies, but without a checker, router, summarizer, or mood write. Its fired intent is the current trigger; the conversation history is prior context. Delivery waits behind reactive replies and timed silence; indefinite mute fails the attempt. Jobs coalesce missed occurrences, allow one instance per schedule, and use a 60-second misfire grace period. Shutdown drains active outreach and schedule mutations before disconnecting Telegram.

### Reply workflow

```mermaid
flowchart LR
    S((start)) -->|latest history| C[checker]
    C -->|no new user messages| E((end))
    C -->|forced admission| A[admission]
    C -->|ordinary turn| R[JEV decision router]
    R -->|stay silent| E
    R -->|reply| A
    A -->|undivided history| SUM[summarizer]
    A -->|new turn and participants| CTX[context fetcher]
    SUM -->|summary and current mood| RESP[responder]
    CTX -->|raw tool observations| RESP
    RESP -->|respond or retrieval limit| E
    RESP -->|missing supported context| CTX
```

The checker rejects turns without new user messages even when admission is forced. Otherwise, direct mentions/replies and the spontaneity path can bypass routing. The router sees the summary and latest 15 messages, uses separate DM/group questions, and admits a reply at a validated `noul` probability of at least **0.7**. Errors or invalid answers admit the turn; rejection skips summarization, retrieval, and generation.

After admission, the summarizer runs once in parallel with the initial context fetch. The responder joins both branches and receives the current call's updated mood and summary when available, active traits, Singapore time, and selected tool observations. Summarizer failure preserves existing state. Each reply has a one-sentence reason stored beside its visible text in history, without exposing structured handoff fields to Telegram.

The responder can request another pass for supported calendar, memory, URL, or proactive-schedule work. Only the context fetcher selects and executes tools, sequentially under each pass's shared 30-second deadline. Raw observations accumulate across passes, including empty results, errors, and timeouts. Memory and schedule calls may repeat within the pass budget; normalized URL attempts are deduplicated across the reply. Personal lookup uses `search_user_info(name, query)`, resolves names against actual participants, and retrieves both scoped graph facts and the structured profile; unknown or ambiguous names produce an error observation.

Set `AGENT_MAX_CONTEXT_PASSES` to **1–10** (default **3**, including the initial fetch). Retrieval stops on a sufficient response, the pass cap, or an approaching graph recursion limit. When more context is still needed, Rachel sends the responder's prepared best-effort clarification. Prometheus exposes `rachel_agent_loop_outcomes_total{outcome}` (`first_pass`, `retried`, `capped`) and `rachel_agent_context_passes` alongside per-node LLM metrics.

The Mermaid above documents the current reply flow. The PNG renderer registers four workflows, but its required `scripts/graph_llm.mmd` source is absent from this checkout. Consequently, `scripts.draw_graphs` fails and `GET /architecture` returns HTTP 502 until that file is restored. The checked-in workflow PNGs may be stale.

### URL reading

`fetch_url` performs GET requests for public HTML, plain text, Markdown, and JSON, including JSON media types ending in `+json`. HTML is converted to readable Markdown with links and tables; extraction runs off the event loop. Results include the requested/final URL, media type, optional title, source content, and a truncation flag.

- At most **two distinct normalized URL attempts per reply**, across all retrieval passes; successful and failed attempts are reused.
- A **10-second** overall fetch deadline, at most **three redirects**, a **2 MiB** decoded response limit, and **12,000 characters** of returned content.
- Standard HTTP/HTTPS ports only; credentials in URLs, private or special-purpose destinations, and DNS answers containing non-public addresses are rejected. Redirect targets are revalidated and connections use a pinned address.
- Fetches do not use environment proxies or application authentication; cookies do not carry between redirect hops.

This is a source reader, without web search, crawling, JavaScript execution, login, PDF parsing, or binary downloads. URL payloads are explicitly labeled untrusted and supplied separately from system instructions.

### Memory workflows

When a conversation is idle for three minutes—or its buffer reaches 150 messages—the transcript is persisted and both memory workflows start in the background.

- **World view:** extract durable, non-personal knowledge, then ingest each fact as a Graphiti episode in the `worldview` partition.
- **User memory:** fan out into free-form personal facts stored in `user-facts-<user_id>` Graphiti partitions and fixed profile slots merged into PostgreSQL JSONB.

History uses the composite key `(chat_id, telegram_message_id)`, since Telegram message IDs are unique only within a chat. Reply context is capped at the latest 40 messages. Profiles use a five-minute cache with write-through after extraction; direct admin edits can remain stale for that TTL. Finalization advances the durable extraction watermark when handing off the snapshot, so failed extraction is not automatically retried. Shutdown flushes buffered history and pending state without starting new extraction.

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Runtime | Python 3.11+ · asyncio | One event loop for HTTP, Telegram, and workflow tasks |
| HTTP service | FastAPI · Uvicorn | Lifespan management, admin API, dashboard, health, and metrics |
| Messaging | Telethon / MTProto | Persistent Rachel and admin-bot Telegram clients without webhooks |
| Orchestration | LangGraph · LangChain OpenAI | Stateful reply and memory workflows over an OpenAI-compatible endpoint |
| Scheduling | APScheduler 3 · IANA timezones | In-process cron jobs backed by durable PostgreSQL definitions |
| Model gateway | Merge Gateway | Chat completions, JEV reply decisions, and embeddings |
| Long-term memory | Graphiti · Neo4j 5.26 | Temporal entity graph, conflict handling, and hybrid RRF search |
| Operational data | PostgreSQL 16 · SQLAlchemy 2 · asyncpg | Histories, summaries, moods, watermarks, profiles, traits, activities, outreach, and active models |
| Schema changes | Alembic | Versioned PostgreSQL migrations |
| Structured data | Pydantic 2 · pydantic-settings | Validated model output, API bodies, and environment configuration |
| Public URL content | HTTPX · Trafilatura | Bounded HTTP retrieval and HTML-to-Markdown extraction |
| Observability | Prometheus · Phoenix OTEL · OpenInference | Model/loop metrics, workflow traces, router decisions, and tool evidence |
| Admin UI | Vanilla HTML/CSS/JavaScript | Single-file dashboard with no frontend build step |
| Packaging | uv · `pyproject.toml` · `uv.lock` | Reproducible dependency installation and command execution |
| Infrastructure | Docker · Docker Compose | Local/production PostgreSQL, Neo4j, and optional app container |

## Features

- Human-like reply timing, typing indicators, and multi-message bursts
- Group-aware reply routing, 5% spontaneous participation, timed silence, and indefinite mute controls
- Media placeholders for stickers, GIFs, photos, videos, voice messages, audio, and files
- Seven conversation moods, with successful summarizer updates available in the current reply
- Twelve live-tunable personality sliders and a seeded weekly schedule in Singapore time
- Bounded retrieval across calendar, world-view, user facts/profiles, public URLs, and schedule tools
- Proactive reminders and check-ins with cron, timezone, lifetime attempt limits, and run status
- Chat-scoped schedule creation and management through conversation
- Temporal world and per-user knowledge graphs with scoped Graphiti group IDs
- Sixteen-slot structured user profiles with deterministic conflict-free merging
- Runtime model catalog and independent main/small/embedding model roles
- Admin control through Telegram, REST, and a browser dashboard
- Read-only inspection of source-controlled workflow prompts
- Per-reply reasoning stored beside the Telegram message history
- Prometheus metrics for model reliability and loop outcomes, plus Phoenix workflow/tool tracing
- Graceful shutdown that drains outreach and schedule mutations, then flushes conversation buffers
- One-off scripts for graph seeding, visualization, login, and destructive graph reset

## Future Potential

Possible extensions, rather than committed roadmap items, include durable background-extraction retries and expiry policies for short-lived facts such as “Sarah's interview is on Thursday.” These would address the current handoff watermark tradeoff and distinguish events from enduring preferences. Proactive outreach already exists; a future calendar integration could update or cancel reminders when an external event changes.

Discord or WhatsApp Business adapters could bring the persona and memory workflows to other communities, with new work needed for transport identity, permissions, message ordering, and delivery semantics. Reusable persona packages could bundle prompts, traits, activities, profile schemas, and memory policies for game characters, interactive fiction, education, or community assistants.

Runtime model selection, stored response reasons, retrieval observations in traces, and per-node metrics also provide a foundation for evaluating persona consistency, factual recall, and model reliability. An evaluation harness would need to control datasets and embedding compatibility; it is not included today.

---

## Getting Started

### Prerequisites

Install these tools before cloning the repository:

| Tool | Version | Install |
|---|---|---|
| Git | Any maintained release | [git-scm.com](https://git-scm.com/downloads) |
| Python | 3.11 or newer | [python.org](https://www.python.org/downloads/) |
| uv | Current stable | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Docker Desktop / Docker Engine | Compose v2 capable | [docs.docker.com](https://docs.docker.com/get-docker/) |
| Telegram account | Able to receive a login code | Used to create Rachel's persistent Telethon session |

Use a separate BotFather bot for the admin client; Rachel's speaking identity is authorized independently in `anon.session` and can be a bot token or a user-account login. Node.js is optional and needed only for the dashboard's built-in Node test runner. There is no frontend dependency installation or build step.

Docker must be running before PostgreSQL or Neo4j can start. Verify it with `docker ps`; a table—even an empty one—means the daemon is reachable.

### 1. Clone the repository

Clone the GitHub repository and enter its root directory:

```bash
git clone https://github.com/GeneralR3d/Rachael.git
cd Rachael
```

All following commands assume the current directory is the repository root.

### 2. Install Python dependencies

Let uv create `.venv` and install the exact dependency set recorded in `uv.lock`:

```bash
uv sync --frozen
```

uv should finish with the locked dependencies installed in `.venv`, or report that they are already installed. If Python 3.11+ is missing, run `uv python install 3.11` and repeat the command.

### 3. Configure the environment

Copy the committed template to the local file read by `pydantic-settings` and Docker Compose:

```bash
cp template.env .env
```

Never commit `.env`; it contains Telegram and gateway credentials.

#### Telegram credentials

**`TELEGRAM_API_ID`** and **`TELEGRAM_API_HASH`** *(required)*
Identify your Telegram developer application to MTProto. Create an application at [my.telegram.org](https://my.telegram.org) under **API development tools**.
Example: `TELEGRAM_API_ID=1234567` and `TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef`

**`TELEGRAM_BOT_TOKEN`** *(required)*
Authenticates the separate admin bot. Create it with [@BotFather](https://t.me/botfather) using `/newbot`.
Example: `TELEGRAM_BOT_TOKEN=123456789:AAExampleToken`

**`ADMIN_ID`** *(required)*
Whitelists the only Telegram user allowed to operate the admin bot. Obtain your numeric user ID from [@userinfobot](https://t.me/userinfobot); leaving the template value blank prevents the integer setting from validating at startup.
Example: `ADMIN_ID=987654321`

Rachel's own Telegram login is intentionally not stored in `.env`; step 6 creates `anon.session` interactively.

#### Merge Gateway and model roles

**`MERGE_GATEWAY_API_KEY`** *(required)*
Authenticates JEV reply routing, summarization, context selection, user-fact extraction, and profile extraction. Create a Gateway credential at [gateway.merge.dev](https://gateway.merge.dev).
Example: `MERGE_GATEWAY_API_KEY=mg_...`

**`MERGE_GATEWAY_GRAPHITI_API_KEY`** *(optional)*
Separately meters the world-view extractor and Graphiti's internal LLM, embedding, and reranking calls. Leave it blank to reuse `MERGE_GATEWAY_API_KEY`.
Example: `MERGE_GATEWAY_GRAPHITI_API_KEY=mg_...`

**`MERGE_GATEWAY_BASE_URL`** *(optional)*
Gateway-native base URL used by Graphiti embeddings and the router's `/decisions` request. Keep the default unless you operate a custom endpoint.
Default: `https://api-gateway.merge.dev/v1`

**`MERGE_GATEWAY_OPENAI_BASE_URL`** *(optional)*
OpenAI-compatible base URL used by chat completions and reranking. The `/v1/openai` suffix is required for the hosted Gateway.
Default: `https://api-gateway.merge.dev/v1/openai`

**`LLM_MODEL`** *(optional)*
Initial model for the chat reply workflows, extractors, and Graphiti's main role. The value must use Gateway's `provider/model` format.
Default: `deepseek/deepseek-v4-flash`

**`DECISIONS_LLM`** *(optional)*
Decision model for the router's reply gate. The router asks one `should_reply` Noul question through `/v1/decisions` and replies when its probability is at least 0.7. Invalid answers and request errors default to replying.
Default: `typesafe/jev-1.13`

**`LLM_SMALL_MODEL`** *(optional; supported by the code but not currently listed in `template.env`)*
Initial smaller model for Graphiti helper and reranker calls.
Default: `deepseek/deepseek-v4-flash`

**`LLM_EMBEDDING_MODEL`** *(optional)*
Initial embedding model for Graphiti's semantic search.
Default: `openai/text-embedding-3-small`

**`AGENT_MAX_CONTEXT_PASSES`** *(optional)*
Sets the total retrieval-pass budget for each reactive or proactive reply, including the initial pass. Choose an integer from **1–10**; values outside that range fail settings validation.
Default: `3`

The database-backed model selector can override the main, small, and embedding values at runtime. Startup seeds missing selections without overwriting existing admin choices. Legacy `OPENROUTER_MODEL`, `OPENROUTER_SMALL_MODEL`, and `OPENROUTER_EMBEDDING_MODEL` names are accepted only as compatibility aliases for model IDs; the commented `OPENROUTER_API_KEY`, `OPENROUTER_GRAPHITI_API_KEY`, and `OPENROUTER_BASE_URL` template entries are retired and have no effect.

Model IDs above are repository defaults, not a guarantee that every Gateway route supports the required features. The summarizer and three memory extractors use function calling with reasoning disabled; the context fetcher binds real tools with medium reasoning; the responder requests strict JSON Schema output with medium reasoning. Preserve these capability requirements when choosing another main model. Graphiti is configured with a small-model reranker, but the current RRF search does not invoke it because its log-probability requirements are incompatible with the Gateway response.

#### Persona labels

**`BOT_NAME`** *(optional)*
Controls Rachel's display label in prompts and the exact name accepted by natural-language silence commands.
Default: `Rachel`

**`USER_NAME`** *(optional)*
Loaded for legacy compatibility, but does not currently affect conversation behavior. It may be left blank.

#### PostgreSQL

**`DB_PASSWORD`** *(optional locally; change in production)*
Sets the password used by the Compose PostgreSQL container. Keep it above `DATABASE_URL` because dotenv interpolation is top-down. URL-encode characters such as `@`, `:`, `/`, or `#` inside the URL.
Local default: `rachel`

**`DATABASE_URL`** *(required to match the chosen run mode)*
Async SQLAlchemy connection string. For a host-run app talking to the Compose database, use port `5433`:
`DATABASE_URL=postgresql+asyncpg://rachel:${DB_PASSWORD}@localhost:5433/rachel`

For the Compose `app` service, the compose file overrides this with the internal host `db:5432` automatically. The bare default in `app/config.py` uses localhost port `5432`, so keep the template's explicit `5433` URL for a host-run app using the Compose database.

#### Neo4j

**`NEO4J_PASSWORD`** *(required)*
Must match the password used in `NEO4J_AUTH`. Set a non-default value, especially outside local development.
Example: `NEO4J_PASSWORD=replace-with-a-long-random-password`

**`NEO4J_USER`** *(optional)*
Neo4j account name.
Default: `neo4j`

**`NEO4J_URI`** *(required to match the chosen run mode)*
Use `bolt://localhost:7687` when the app runs on the host. The Compose `app` service overrides it with `bolt://neo4j:7687`.

**`WORLDVIEW_PATH`** *(legacy, optional; not in the template)*
Defaults to `worldview.md`, but does not control active fact storage. World knowledge lives in Neo4j, and the bulk import script reads the repository-root `worldview.md` directly.

#### Phoenix tracing

**`PHOENIX_ROOT_URL`** *(optional, commented in the template)*
Sets the public base URL used by the Compose Phoenix service when generating links. Supply your own reverse-proxy URL for a server deployment; the Compose default is `http://localhost:6006`. This setting does not choose the app's trace destination.
Example: `PHOENIX_ROOT_URL=https://phoenix.example.com`

**`PHOENIX_COLLECTOR_ENDPOINT`** and **`PHOENIX_ENDPOINT`** *(optional, SDK/process settings; not in the template)*
The Compose app service supplies both as `http://phoenix:6006`. For a host-run app, explicitly supply `http://localhost:6006` in the process environment as shown below. Phoenix registration happens before application settings load `.env`, so adding these keys only to `.env` is insufficient for that early registration.

**`PYTHONUNBUFFERED`** *(optional, container setting)*
The Compose app sets this to `1` so Python logs reach container output promptly. No manual value is needed for the local setup.

### 4. Start PostgreSQL and Neo4j

Start only the two local infrastructure services while running the Python app on the host:

```bash
docker compose up -d db neo4j
```

Check that both containers reach a healthy state:

```bash
docker compose ps
```

You should see `rachel-db` and `rachel-neo4j` as healthy. PostgreSQL is available only on `127.0.0.1:5433`; Neo4j Bolt is on `127.0.0.1:7687`, and the Neo4j browser is at [http://localhost:7474](http://localhost:7474). First-time Neo4j startup can take 20–40 seconds.

If a container stays unhealthy, inspect it with `docker compose logs db` or `docker compose logs neo4j` and confirm the passwords in `.env` match the compose configuration.

### 5. Initialize PostgreSQL

Apply every Alembic migration to create or upgrade the application schema:

```bash
uv run alembic upgrade head
```

Alembic should report each revision and finish without an error. Run this again after pulling migrations. Neo4j does not use Alembic; Graphiti creates its indices and constraints on first access.

### 6. Create Rachel's Telegram session

Run the one-time interactive login that writes the gitignored `anon.session` file:

```bash
uv run python -m scripts.login
```

At the phone-number-or-bot-token prompt, enter the separate token for the bot that should speak as Rachel, or use a user-account phone number and complete Telegram's login challenge. This identity is independent of `TELEGRAM_BOT_TOKEN`, which belongs to the admin bot. A successful run prints `Logged in as: ... Session saved to anon.session.` Complete this step before Uvicorn: without an authorized session, startup attempts to authenticate `anon.session` with the configured admin bot token.

### 7. Run Rachel

Start FastAPI with hot reload for local development:

```bash
uv run uvicorn app.main:app --reload
```

Startup seeds traits, weekly activities, and missing model selections, starts both Telegram clients, then restores proactive jobs. Prompts are imported from `app/prompts.py`. The service exposes:

- Dashboard: [http://localhost:8000/](http://localhost:8000/)
- Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- Prometheus metrics: [http://localhost:8000/metrics](http://localhost:8000/metrics)

Uvicorn should report `Application startup complete.` If startup fails, first verify the database health and URLs, required `.env` values, and the authorized `anon.session`. The admin client's `bot.session` is created from `TELEGRAM_BOT_TOKEN` on first startup.

For local traces, start the optional Phoenix service:

```bash
docker compose up -d phoenix
```

The Phoenix UI should become available at [http://localhost:6006](http://localhost:6006). Start or restart the app with its trace endpoints explicitly set in the process environment:

```bash
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006 PHOENIX_ENDPOINT=http://localhost:6006 uv run uvicorn app.main:app --reload
```

New model and workflow calls should appear under the `Rachel` project. If trace export fails, check `docker compose logs phoenix` and confirm the endpoint matches the app's run mode.

### Verify the service

Call the lightweight health endpoint:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"ok"}
```

The health route confirms HTTP startup; it does not probe Telegram, either datastore, or a model call. Confirm the model metrics are exposed, following the metrics mount's redirect if present:

```bash
curl -fsSL http://localhost:8000/metrics/ | rg 'rachel_(llm|agent)'
```

The command should print metric descriptions and histogram samples; model-counter labels appear after calls occur. Use `grep -E` in place of `rg` if ripgrep is unavailable. Finally, mention Rachel or reply directly to one of her messages from another Telegram account and wait for the seven-second debounce. An ordinary DM may correctly be suppressed by the router.

### Optional: seed long-term memory

Add one curated general fact through the same Graphiti ingestion path used in production:

```bash
uv run python -m scripts.add_worldview_fact "Chagee is a bubble tea brand"
```

Bulk-ingest `- ` bullet lines from a repository-root `worldview.md` file:

```bash
uv run python -m scripts.ingest_worldview_md
```

Bulk-ingest `- ` bullet lines from `user_fact_<user_id>.md` into one user's partition:

```bash
uv run python -m scripts.ingest_user_facts_md 123456789
```

Ingestion is intentionally sequential and may take several model round-trips per fact. Re-running a bulk script creates additional raw episodes even when Graphiti deduplicates the derived entities and edges.

## Group-Chat Behavior

Rachel considers each non-empty incoming turn, but speaking behavior depends on context:

- A direct @mention or reply latches forced admission for the whole burst and clears both timed silence and indefinite mute. The no-new-user-message guard still applies.
- Ordinary group messages use the group router; DMs use a separate private-message router that can suppress acknowledgements or already-handled content.
- Untagged turns have a 5% chance to skip the router for spontaneous participation.
- `Rachel shush`, `Rachel quiet`, `Rachel silence`, `Rachel shut up`, or `Rachel stop talking` silences a group for 15 minutes.
- `Rachel mute` silences a group indefinitely; `Rachel speak` resumes normal scheduling.
- Silence state is process-local and does not survive an application restart. Messages still persist and feed memory while Rachel is silent.

## Admin and Operations

### Browser dashboard

The single-file dashboard at `/` exposes prompts, personality traits, model selections, proactive schedules, chats, histories, user profiles and facts, and world-view facts. Chat names resolve through Telegram, with unavailable names falling back to IDs in the UI. Its Architecture tab currently reports a rendering failure because the Mermaid source is missing. The HTTP API, dashboard, and metrics have no application authentication; use loopback locally and an authenticated reverse proxy in production.

System prompts are defined in `app/prompts.py` and are read-only in the dashboard, API, and Telegram admin bot. Edit the source and restart the service to change them. Migration `f9c6a1d4b3e8` drops the retired `system_prompts` table and its stored values. Personality levels remain adjustable.

### Telegram admin bot

Only `ADMIN_ID` can use these command groups:

| Area | Commands |
|---|---|
| Prompts (read-only) | `/get_responder_system_prompt`, `/get_summarizer_system_prompt` |
| Chats | `/list_user_names`, `/list_chats`, `/get_history <chat_id>`, `/clear_history <chat_id>`, `/get_summary <chat_id>`, `/delete_summary <chat_id>` |
| User memory | `/get_user_facts <user_id>`, `/add_user_facts <user_id> <fact>`, `/get_user_profile <user_id>`, `/delete_user_profile <user_id>` |
| Personality | `/list_traits`, `/set_trait <id> <low\|medium\|high>`, `/reset_traits` |
| Models | `/list_models`, `/list_active_models`, `/add_model <provider/model>`, `/delete_model <provider/model>`, `/set_active_model <main\|small\|embedding> <provider/model>` |

### REST API

Routes are mounted at the root, with [app/routers/admin.py](app/routers/admin.py) as the schema source of truth. Open [http://localhost:8000/docs](http://localhost:8000/docs) for request bodies and generated API documentation.

- **Prompts:** `GET /responder-system-prompt`, `/summarizer-system-prompt`, and `/workflow-prompts` are read-only.
- **Users and chats:** `GET /users/names` and `/list-chats`.
- **Conversation state:** `GET` and `DELETE /history/{chat_id}` and `/summary/{chat_id}`.
- **Profiles:** `GET`, `PUT`, and `DELETE /user-profile/{user_id}`; `GET /user-profile-fields` derives fields from the source schema.
- **Facts:** `GET` and `POST /user-facts/{user_id}` and `/worldview-facts`. There is no episode edit/delete API; ingestion may take several model round-trips per fact.
- **Traits:** `GET /personality`, `PATCH /personality/{trait_id}`, and `POST /personality/reset`.
- **Models:** `GET`, `POST`, and `DELETE /models` (delete takes a `model_string` query parameter); `GET /active-models` and `PUT /active-models/{role}`.
- **Outreach:** `GET` and `POST /proactive-schedules`; `PUT` and `DELETE /proactive-schedules/{schedule_id}`; `PATCH /proactive-schedules/{schedule_id}/enabled`; `POST /proactive-schedules/{schedule_id}/run`. Responses include attempts consumed, status/error, and next run time.
- **Diagnostics:** `GET /health`, `GET /architecture` (currently HTTP 502), and the `/metrics` ASGI mount.

### Prometheus metrics

- `rachel_llm_calls_total{node}` counts logical model calls by workflow node.
- `rachel_llm_errors_total{node,kind}` counts final failures by node and bounded error class.
- `rachel_agent_loop_outcomes_total{outcome}` records `first_pass`, `retried`, or `capped` for admitted reactive and proactive loops.
- `rachel_agent_context_passes` records retrieval pass counts in histogram buckets from 1–10.

The model counters cover router, summarizer, context fetcher, responder, the three memory extractors, and internal Graphiti LLM calls. Recovered Graphiti retries are not terminal errors. Embedding/reranking calls and non-LLM infrastructure failures are outside those counters. Metrics use the single-process registry; run one application process.

### Phoenix traces

`app/main.py` registers the `Rachel` Phoenix project with HTTP/protobuf export and automatic instrumentation. Workflow roots are `telegram_reply`, `telegram_proactive`, and `memory_update`, grouped where available by burst-session ID and chat metadata. The direct router request has a manual LLM span; direct world-view and personal-memory lookups have explicit tool spans under the context fetcher, while calendar, URL, and schedule tools retain their LangChain spans.

Tool spans include arguments, complete results, retrieval pass/category/status, and errors or deadline cancellations. This includes retrieved personal facts and profiles, so trace storage is another copy of conversation-derived data. The optional Compose Phoenix service stores its data in the `phoenix_data` volume and exposes its UI only on `127.0.0.1:6006`.

## Development

Run the server with source reload:

```bash
uv run uvicorn app.main:app --reload
```

Apply all pending migrations:

```bash
uv run alembic upgrade head
```

Create a blank migration after changing SQLAlchemy models, then edit the generated revision explicitly:

```bash
uv run alembic revision -m "describe the schema change"
```

Inspect the migration graph's head without applying a schema change:

```bash
uv run alembic heads
```

The current head is `0a7e2c9d4b61`. Preserve existing revision IDs and ancestry when adding a migration; `alembic revision` above creates a blank file that must be edited explicitly.

Run a lightweight Python syntax check without starting databases or Telegram:

```bash
uv run python -m compileall -q app scripts alembic
```

Run Python regression tests with valid app settings. Model and Telegram boundaries are mocked; placeholder API credentials suffice, and no Telegram messages are sent:

```bash
uv run python -m unittest discover -s tests
```

Run just the bounded-agent-loop suite when working on retrieval orchestration:

```bash
uv run python -m unittest tests.test_agent_loop
```

Run the dashboard JavaScript checks with Node's built-in test runner; no browser or frontend build is needed:

```bash
node --test tests/test_proactive_dashboard.cjs
```

Successful test commands finish with no failures; database tests report skips unless configured. **`TEST_DATABASE_URL`** is an optional test-only async PostgreSQL URL, for example `postgresql+asyncpg://rachel:rachel@localhost:5433/rachel_test`. Create a disposable database yourself before setting it; Compose only creates the application database. Integration tests create and remove isolated schemas. Never point this setting at production data.

Tests cover reply routing, responder schemas/fallbacks, prompt boundaries, public URL handling, Telegram lifecycle races, schedule tools and limits, scheduler shutdown, tracing, and migration preservation. Migration `0a7e2c9d4b61` adds the history timestamp if absent while preserving existing timestamps. There is no lint configuration or CI workflow in this checkout. `Reference/` is an ignored local copy of the original implementation and may be absent from a fresh clone.

The workflow renderer registers reactive, proactive, world-view, and user-memory diagrams. After restoring its missing `scripts/graph_llm.mmd` source, the following command writes all four PNGs; Mermaid rendering may also require network access:

```bash
uv run python -m scripts.draw_graphs
```

A successful render prints `wrote <filename>` for each graph. In the current checkout it fails on the absent source; use the inline Mermaid in this README to understand the reply flow.

`scripts.clear_graph` deletes **every Neo4j partition**, including all personal memories. It requires the exact typed confirmation `delete everything` unless `--yes` is supplied. Use the additive seeding helpers above for ordinary imports.

## Deployment

Two deployment guides are included:

- [`DEPLOY.md`](DEPLOY.md) runs PostgreSQL and Neo4j in Docker while the app runs on the host under systemd.
- [`DEPLOY_DOCKER.md`](DEPLOY_DOCKER.md) runs the app and both datastores with Docker Compose.

Use this README and current code when older guide instructions disagree: the host guide contains retired OpenRouter names and omits Neo4j from some commands, and both guides have older prompt-seeding/login descriptions. Both datastores are needed; use the `MERGE_GATEWAY_*` and `LLM_*` settings above. The Compose `app` service is already enabled, injects internal database/Phoenix addresses, and applies Alembic migrations before serving. Its session bind mounts require actual files on the host before container startup.

Run **one app process** in either topology; multiple workers would duplicate Telegram state and proactive scheduling. Bind host-run Uvicorn to loopback and protect the dashboard, REST API, metrics, and exposed Phoenix UI with nginx basic authentication or an equivalent access layer.

## Known Limitations

- Telethon sessions, buffers, locks, timers, caches, APScheduler jobs, and metrics are process-local. Multiple app workers or replicas require coordination that is not implemented.
- A multi-burst reply is persisted as one history row keyed to the first Telegram message ID, so an interleaved user message can be ordered differently after a database re-seed.
- Timed silence, indefinite mute state, and reply watermarks are process-local; the durable memory-extraction watermark is persisted.
- Extraction watermarks advance at background-task handoff. Failed extraction is not automatically replayed, and shutdown flushes do not start memory extraction.
- Proactive repeat limits count attempts, including failures and manual runs. They do not guarantee successful delivery; missed occurrences beyond the scheduler grace period are not replayed.
- The missing `scripts/graph_llm.mmd` prevents CLI diagram generation and makes `/architecture` return HTTP 502; checked-in PNGs may be stale.
- Switching to an embedding model with a different vector dimensionality does not automatically re-embed existing Neo4j data.
- Graphiti's configured OpenAI reranker requires log probabilities the Gateway does not supply. Current RRF retrieval avoids it; changing search recipes needs compatibility work.
- Admin profile edits can remain stale in the five-minute profile cache until its TTL expires.
- URL reading is limited to public static text/HTML/Markdown/JSON and is subject to the fetch bounds above.
- The admin HTTP surface does not implement its own authentication.
- Linting, CI, and a ready-made Prometheus/Grafana deployment are not yet included.

## License

No license file is currently included. Contact the maintainer before redistributing the code or creating derivative works.
