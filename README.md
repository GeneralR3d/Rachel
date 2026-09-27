# Rachel

> A stateful Telegram persona that remembers people, reads the room, and texts with human timing instead of behaving like a command-driven chatbot.

![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Neo4j](https://img.shields.io/badge/Neo4j-Graphiti-4581C3?logo=neo4j&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-stateful%20workflows-F97316)
![Merge Gateway](https://img.shields.io/badge/LLM-Merge%20Gateway-111827)

## What is Rachel?

Most chatbots forget who they are talking to, answer every message immediately, and become noisy the moment they enter a group chat. Rachel is designed around the opposite goal: feel like a believable participant who knows when to speak, remembers what matters, and carries context across conversations.

The shipped persona is Rachel, a 22-year-old NTU marketing student in Singapore with a backstory, seven conversational moods, twelve tunable personality traits, and a weekly schedule. She waits for a burst of messages to finish, types at a human pace, can stay quiet when a conversation does not concern her, and recalls both general world knowledge and personal facts about the people she meets.

Underneath the character is an asynchronous persona engine: FastAPI hosts two Telethon clients, LangGraph coordinates reply and memory workflows, PostgreSQL stores operational state, and Graphiti on Neo4j provides temporal long-term memory. All model and embedding traffic is routed through Merge Gateway. The architecture is reusable beyond one character—the persona, tone, schedule, prompts, memories, and active models are data rather than hard-coded application flow.

## Key Innovations

### 1. Human cadence with race-safe delivery

Rachel does not answer directly inside Telegram's message-arrival event. Every new message resets a seven-second reply debounce and a three-minute conversation-finalization timer, allowing a burst of texts to become one coherent turn. Multi-paragraph answers are emitted as separate Telegram messages with simulated typing time, while per-chat locks, shielded in-flight replies, causal watermarks, and message-ID-ordered buffer insertion prevent overlapping or repeated responses when new messages arrive mid-generation.

### 2. Layered participation instead of “always reply”

Direct mentions and replies can bypass the LLM gate, while ordinary group messages and DMs pass through purpose-specific router prompts that decide whether a response adds value. Untagged conversations also have a small spontaneity path, so Rachel can occasionally join naturally without dominating the room. Operators can issue strict natural-language controls—`Rachel shush` for a 15-minute silence, `Rachel mute` for indefinite silence, and `Rachel speak` to resume—without stopping persistence or memory extraction.

### 3. Selective retrieval in parallel with summarization

The responder never receives the entire schedule or memory store by default. A single-pass tool-calling node decides whether the current turn needs calendar context, world knowledge, or a particular participant's memories; all requested tools run once, and their results are routed into typed state fields. This retrieval branch runs in parallel with mood/summary generation and has a 30-second fail-open timeout, so useful memory improves a reply without becoming a reliability bottleneck.

### 4. Two causal boundaries for two different jobs

Reply generation and memory extraction use separate notions of “already handled.” The reply graph partitions history with an in-memory responded watermark so a message received during an earlier reply cannot be accidentally answered twice. The memory workflows use a persisted PostgreSQL high-water mark, insert a divider into the full transcript, and extract only from messages below it—preserving old context while ensuring facts are not mined again after a flush or restart.

### 5. Temporal knowledge-graph memory with hybrid retrieval

Finished conversations produce general world facts and per-user personal facts, each ingested as a Graphiti episode under a scoped group ID. Graphiti performs entity resolution, structural deduplication, and temporal edge invalidation so newer contradictory facts can supersede older ones. Retrieval combines semantic, BM25, and graph signals with reciprocal-rank fusion, then returns atomic relationship edges and verbatim episodes rather than injecting a whole memory store into every prompt.

### 6. Different memory types get different merge strategies

Open-ended facts benefit from Graphiti's LLM-assisted entity and conflict resolution. A user's 16 fixed profile slots do not: those values live in one PostgreSQL JSONB document and merge deterministically under a per-user lock, where newer non-empty values win. The slot schema is defined once and reused to construct Pydantic output models, render prompts, validate extracted data, and drive the admin UI—adding a profile field does not require a database migration.

### 7. Runtime model control plus gateway-specific reliability

The main, small, and embedding model roles are stored in PostgreSQL and can be switched at runtime from Telegram, REST, or the dashboard. Cache invalidation rebuilds the LangChain clients and Graphiti singleton on the next call, so no process restart is required. Graphiti also uses a custom retrying OpenAI-compatible client and `json_object` structured output to recover from valid JSON whose shape does not match Graphiti's Pydantic schemas—a common failure mode that transport retries alone cannot fix.

### 8. Failure-aware observability that matches product behavior

LLM calls and final failures are counted at the same fail-open boundaries used by the product. Prometheus counters label calls by workflow node and classify errors into a bounded vocabulary such as `timeout`, `rate_limit_429`, `upstream_504`, `output_parse`, and `response_validation`. That makes it possible to compare model reliability and cost by pipeline stage without exploding metric cardinality or counting recovered retries as incidents.

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
        BUF[(Per-chat buffers · timers · locks)]
    end

    subgraph AI[LangGraph workflows]
        REPLY[Reply gate · summary · retrieval · response]
        MEM[World-view and user-memory extraction]
    end

    GW[Merge Gateway<br/>chat · embeddings · reranking]
    PG[(PostgreSQL<br/>history · state · profiles · models)]
    N4J[(Neo4j + Graphiti<br/>temporal memories)]

    U -->|Telegram events| RC
    RC -->|debounced turns| BUF
    BUF -->|unanswered messages| REPLY
    REPLY -->|typed message bursts| U
    BUF -->|finalized transcript| MEM
    A <-->|admin commands| BOT
    A <-->|dashboard and REST| API
    REPLY <-->|prompts · summaries · profiles| PG
    BUF <-->|seed and flush history| PG
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
- **Memory workflows** (`app/services/memory.py`, `worldview.py`, `userfacts.py`) partition finalized transcripts once, then update general knowledge, personal facts, and structured profiles concurrently.
- **Graphiti adapter** (`app/services/graphiti.py`) initializes Neo4j indices, serializes graph writes, performs hybrid retrieval, and adds schema-aware retry behavior around Graphiti's LLM client.
- **Data layer** (`app/models.py`, `app/repository.py`, `app/database.py`) uses async SQLAlchemy sessions and PostgreSQL upserts for durable chat and control-plane state.
- **Admin plane** (`app/telegram/bot.py`, `app/routers/admin.py`, `app/static/index.html`) exposes Telegram commands, REST endpoints, a zero-build dashboard, live architecture diagrams, and Prometheus metrics.

### Reply workflow

```mermaid
flowchart LR
    S((start)) --> C[cheap checker]
    C -->|forced reply| SUM[summarizer]
    C -->|forced reply| CTX[context fetcher]
    C -->|ordinary turn| R[LLM router]
    R -->|stay silent| E((end))
    R -->|reply| SUM
    R -->|reply| CTX
    SUM --> RESP[responder]
    CTX --> RESP
    RESP --> E
```

After the gate passes, the summarizer and context fetcher run in parallel. The responder joins both branches and receives the previous mood, fresh summary, active personality traits, current Singapore time, and only the schedule or memory context selected for this turn. Every sent reply includes a one-sentence reason stored with its history row for debugging.

The compiled graph is checked into the repository:

![Reply workflow](graph_llm.png)

### Memory workflows

When a conversation is idle for three minutes—or its buffer reaches 150 messages—the transcript is persisted and both memory workflows start in the background.

- **World view:** extract durable, non-personal knowledge, then ingest each fact as a Graphiti episode in the `worldview` partition.
- **User memory:** fan out into free-form personal facts stored in `user-facts-<user_id>` Graphiti partitions and fixed profile slots merged into PostgreSQL JSONB.

![World-view workflow](graph_worldview.png)

![User-memory workflow](graph_userfacts.png)

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Runtime | Python 3.11+ · asyncio | One event loop for HTTP, Telegram, and workflow tasks |
| HTTP service | FastAPI · Uvicorn | Lifespan management, admin API, dashboard, health, and metrics |
| Messaging | Telethon / MTProto | Persistent Rachel and admin-bot Telegram clients without webhooks |
| Orchestration | LangGraph · LangChain OpenAI | Stateful reply and memory workflows over an OpenAI-compatible endpoint |
| Model gateway | Merge Gateway | Routes chat, embedding, and reranking requests by `provider/model` ID |
| Long-term memory | Graphiti · Neo4j 5.26 | Temporal entity graph, conflict handling, and hybrid RRF search |
| Operational data | PostgreSQL 16 · SQLAlchemy 2 · asyncpg | Histories, summaries, moods, watermarks, profiles, prompts, traits, schedule, and active models |
| Schema changes | Alembic | Versioned PostgreSQL migrations |
| Structured data | Pydantic 2 · pydantic-settings | Validated model output, API bodies, and environment configuration |
| Observability | Prometheus client | Per-node LLM call and classified error counters at `/metrics` |
| Admin UI | Vanilla HTML/CSS/JavaScript | Single-file dashboard with no frontend build step |
| Packaging | uv · `pyproject.toml` · `uv.lock` | Reproducible dependency installation and command execution |
| Infrastructure | Docker · Docker Compose | Local/production PostgreSQL, Neo4j, and optional app container |

## Features

- Human-like reply timing, typing indicators, and multi-message bursts
- Group-aware reply routing, 5% spontaneous participation, timed silence, and indefinite mute controls
- Media placeholders for stickers, GIFs, photos, videos, voice messages, audio, and files
- Seven persistent conversation moods applied with a deliberate one-call lag
- Twelve live-tunable personality sliders and a seeded weekly schedule in Singapore time
- Selective calendar, world-view, user-fact, and user-profile retrieval per turn
- Temporal world and per-user knowledge graphs with scoped Graphiti group IDs
- Sixteen-slot structured user profiles with deterministic conflict-free merging
- Runtime model catalog and independent main/small/embedding model roles
- Admin control through Telegram, REST, and a browser dashboard
- Read-only workflow prompt inspection and on-demand LangGraph diagram rendering
- Per-reply reasoning stored beside the Telegram message history
- Prometheus metrics with low-cardinality error classification
- Graceful shutdown that flushes in-memory conversation buffers
- One-off scripts for graph seeding, visualization, login, and destructive graph reset

## Future Potential

The next memory layer could separate short-lived episodic facts—“Sarah's interview is on Thursday”—from semantic memories such as “Sarah works in finance.” Graphiti's temporal edge invalidation provides a natural base for expiry, cancellation, and event completion, while a slower impression model could track how Rachel's stance toward each person evolves over time.

The engine can also support multiple characters or transports. Moving to Discord or WhatsApp Business mainly changes the messaging adapter; the reply and memory workflows remain transport-independent. Persona packages could define prompts, traits, schedules, profile schemas, and memory policies for game characters, interactive fiction, education, or community assistants.

Runtime model roles make this a useful evaluation platform as well. A deployment can compare main-model quality, cheaper helper models, and embedding choices without changing workflow code, while stored response reasons and per-node metrics provide the raw material for long-horizon persona-consistency and memory-drift evaluations.

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

You should see uv resolve the project without changing the lockfile. If Python 3.11+ is missing, run `uv python install 3.11` and repeat the command.

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
Authenticates reply routing, summarization, context selection, user-fact extraction, and profile extraction. Create a Gateway credential at [gateway.merge.dev](https://gateway.merge.dev).
Example: `MERGE_GATEWAY_API_KEY=mg_...`

**`MERGE_GATEWAY_GRAPHITI_API_KEY`** *(optional)*
Separately meters the world-view extractor and Graphiti's internal LLM, embedding, and reranking calls. Leave it blank to reuse `MERGE_GATEWAY_API_KEY`.
Example: `MERGE_GATEWAY_GRAPHITI_API_KEY=mg_...`

**`MERGE_GATEWAY_BASE_URL`** *(optional)*
Gateway-native base URL used by Graphiti embeddings. Keep the default unless you operate a custom endpoint.
Default: `https://api-gateway.merge.dev/v1`

**`MERGE_GATEWAY_OPENAI_BASE_URL`** *(optional)*
OpenAI-compatible base URL used by chat completions and reranking. The `/v1/openai` suffix is required for the hosted Gateway.
Default: `https://api-gateway.merge.dev/v1/openai`

**`LLM_MODEL`** *(optional)*
Initial model for the reply workflows, extractors, and Graphiti's main role. The value must use Gateway's `provider/model` format.
Default: `deepseek/deepseek-v4-flash`

**`LLM_SMALL_MODEL`** *(optional; supported by the code but not currently listed in `template.env`)*
Initial smaller model for Graphiti helper and reranker calls.
Default: `deepseek/deepseek-v4-flash`

**`LLM_EMBEDDING_MODEL`** *(optional)*
Initial embedding model for Graphiti's semantic search.
Default: `openai/text-embedding-3-small`

The database-backed model selector can override these three values at runtime. Legacy `OPENROUTER_MODEL`, `OPENROUTER_SMALL_MODEL`, and `OPENROUTER_EMBEDDING_MODEL` names are accepted only as compatibility aliases for the model IDs; the old OpenRouter connection-key settings are no longer used.

#### Persona labels

**`BOT_NAME`** *(optional)*
Controls Rachel's display label in prompts and the exact name accepted by natural-language silence commands.
Default: `Rachel`

**`USER_NAME`** *(optional)*
Provides an owner/user label for summaries inherited from the original implementation. It may be left blank.

#### PostgreSQL

**`DB_PASSWORD`** *(optional locally; change in production)*
Sets the password used by the Compose PostgreSQL container. Keep it above `DATABASE_URL` because dotenv interpolation is top-down. URL-encode characters such as `@`, `:`, `/`, or `#` inside the URL.
Local default: `rachel`

**`DATABASE_URL`** *(required to match the chosen run mode)*
Async SQLAlchemy connection string. For a host-run app talking to the Compose database, use port `5433`:
`DATABASE_URL=postgresql+asyncpg://rachel:${DB_PASSWORD}@localhost:5433/rachel`

For the Compose `app` service, the compose file overrides this with the internal host `db:5432` automatically.

#### Neo4j

**`NEO4J_PASSWORD`** *(required)*
Must match the password used in `NEO4J_AUTH`. Set a non-default value, especially outside local development.
Example: `NEO4J_PASSWORD=replace-with-a-long-random-password`

**`NEO4J_USER`** *(optional)*
Neo4j account name.
Default: `neo4j`

**`NEO4J_URI`** *(required to match the chosen run mode)*
Use `bolt://localhost:7687` when the app runs on the host. The Compose `app` service overrides it with `bolt://neo4j:7687`.

`WORLDVIEW_PATH` still exists as a legacy setting but is not used for active storage; world knowledge now lives in Neo4j.

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

Enter the phone number or bot token for the Telegram identity that should speak as Rachel, then complete Telegram's login challenge. A successful run prints `Logged in as: ... Session saved to anon.session.` If Uvicorn is started without an authorized session, startup may try to authenticate `anon.session` with the admin bot token, so complete this step first.

### 7. Run Rachel

Start FastAPI with hot reload for local development:

```bash
uv run uvicorn app.main:app --reload
```

Startup seeds prompts, traits, the schedule, and the model catalog; starts both Telegram clients; and serves:

- Dashboard: [http://localhost:8000/](http://localhost:8000/)
- Interactive API docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- Prometheus metrics: [http://localhost:8000/metrics](http://localhost:8000/metrics)

You should see `Telethon clients started.` in the logs. If startup fails before that line, first verify PostgreSQL, Neo4j, `.env`, and both Telethon session files.

### Verify the service

Call the lightweight health endpoint:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Confirm the LLM counters are exposed:

```bash
curl -s http://localhost:8000/metrics | grep rachel_llm
```

The command should print the metric descriptions; labeled samples appear after model calls occur. Finally, message Rachel from another Telegram account and wait for the reply debounce.

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

- A direct @mention or reply forces the reply workflow past the router and also clears active silence.
- Ordinary group messages use the group router; DMs use a separate private-message router that can suppress acknowledgements or already-handled content.
- Untagged turns have a 5% chance to skip the router for spontaneous participation.
- `Rachel shush`, `Rachel quiet`, `Rachel silence`, `Rachel shut up`, or `Rachel stop talking` silences a group for 15 minutes.
- `Rachel mute` silences a group indefinitely; `Rachel speak` resumes normal scheduling.
- Silence state is process-local and does not survive an application restart. Messages still persist and feed memory while Rachel is silent.

## Admin and Operations

### Browser dashboard

The single-file dashboard at `/` exposes prompts, personality traits, the model catalog, active model roles, chats, histories, user profiles and facts, world-view facts, and freshly rendered LangGraph diagrams. It has no application-level authentication; keep it on loopback locally and place an authenticated reverse proxy in front of it in production.

### Telegram admin bot

Only `ADMIN_ID` can use these command groups:

| Area | Commands |
|---|---|
| Prompts | `/get_responder_system_prompt`, `/set_responder_system_prompt <text>`, `/get_summarizer_system_prompt`, `/set_summarizer_system_prompt <text>` |
| Chats | `/list_user_names`, `/list_chats`, `/get_history <chat_id>`, `/clear_history <chat_id>`, `/get_summary <chat_id>`, `/delete_summary <chat_id>` |
| User memory | `/get_user_facts <user_id>`, `/add_user_facts <user_id> <fact>`, `/get_user_profile <user_id>`, `/delete_user_profile <user_id>` |
| Personality | `/list_traits`, `/set_trait <id> <low\|medium\|high>`, `/reset_traits` |
| Models | `/list_models`, `/list_active_models`, `/add_model <provider/model>`, `/delete_model <provider/model>`, `/set_active_model <main\|small\|embedding> <provider/model>` |

### REST API

The API mirrors the control plane with endpoints for prompts, workflow prompt inspection, chats, summaries, user facts, profiles, world-view facts, personality traits, model catalog and active roles, architecture diagrams, health, and metrics. Open [http://localhost:8000/docs](http://localhost:8000/docs) for the generated OpenAPI interface.

### Prometheus metrics

- `rachel_llm_calls_total{node}` counts logical model calls by workflow node.
- `rachel_llm_errors_total{node,kind}` counts final failures by node and bounded error class.

The current counters are process-local. Multiple Uvicorn workers require Prometheus client multiprocess configuration, which is not included in this repository.

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

Render the three compiled LangGraph workflows to PNG files. The renderer may require internet access for Mermaid rendering:

```bash
uv run python -m scripts.draw_graphs
```

Run a lightweight Python syntax check without starting databases or Telegram:

```bash
uv run python -m compileall -q app scripts alembic
```

There is currently no automated test suite, lint configuration, or CI workflow in the repository. The original Telethon/SQLite implementation remains under [`Reference/`](Reference/) as a porting reference.

## Deployment

Two deployment guides are included:

- [`DEPLOY.md`](DEPLOY.md) runs PostgreSQL and Neo4j in Docker while the app runs on the host under systemd.
- [`DEPLOY_DOCKER.md`](DEPLOY_DOCKER.md) runs the app and both datastores with Docker Compose.

The guides contain some pre-Merge-Gateway environment names; use the `MERGE_GATEWAY_*` and `LLM_*` configuration documented in this README. In either topology, bind Uvicorn to loopback and protect the unauthenticated dashboard/API with nginx basic authentication or an equivalent access layer.

## Known Limitations

- A documented race in `_flush_chat` can drop a message that arrives between the persistence write and buffer removal.
- A multi-burst reply is persisted as one history row keyed to the first Telegram message ID, so an interleaved user message can be ordered differently after a database re-seed.
- Timed silence, indefinite mute state, and reply watermarks are process-local; the durable memory-extraction watermark is persisted.
- Switching to an embedding model with a different vector dimensionality does not automatically re-embed existing Neo4j data.
- The admin HTTP surface does not implement its own authentication.
- Tests, linting, CI, and a ready-made Prometheus/Grafana deployment are not yet included.

## License

No license file is currently included. Contact the maintainer before redistributing the code or creating derivative works.
