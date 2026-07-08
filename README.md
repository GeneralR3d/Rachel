# Rachel

> A Telegram agent that texts like a real person — a 22-year-old Singaporean university student with moods, memory, a weekly schedule, and a tunable personality — not like a chatbot.

![Python](https://img.shields.io/badge/python-%E2%89%A53.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![Postgres](https://img.shields.io/badge/Postgres-16-336791)
![Neo4j](https://img.shields.io/badge/Neo4j-Graphiti%20knowledge%20graph-018bff)
![LangGraph](https://img.shields.io/badge/LangGraph-stateful%20agents-orange)
![OpenRouter](https://img.shields.io/badge/LLM-OpenRouter-black)

## Who is Rachel?

Most chatbots forget who you are between conversations, and answer every single message whether or not it was meant for them. Drop one into a group chat and it becomes a nuisance within minutes.

Rachel is the opposite. She's a conversational persona — "Rachel," a 22-year-old marketing student at NTU in Singapore, with a backstory, insecurities, a church youth group, a bubble-tea order, and a weekly timetable. She buffers incoming messages and waits a beat before replying, types at a human speed, splits longer thoughts across several messages, and only chimes in when she's actually being addressed. Over time she builds up a **temporal knowledge graph** of the world and of each person she talks to, retrieves from it selectively per conversation, and her tone shifts with the emotional read of the room.

Technically, Rachel is a FastAPI service that runs two Telethon (Telegram) clients on the same asyncio event loop, backed by **Postgres** (history, summaries, profiles, personality, schedule) and **Neo4j via [Graphiti](https://github.com/getzep/graphiti)** (long-term memory as a temporal knowledge graph), with all the language work driven by a set of **LangGraph** state machines over **OpenRouter**. The interesting part isn't that it calls an LLM — it's the machinery around the LLM that makes the output feel like a person texting you back. The longer-term vision is a persona engine: a reusable substrate for believable, stateful AI characters that live inside the messaging apps people already use.

## Key Innovations

### Human-cadence messaging loop
Rachel never replies on the raw message-arrival event. Each incoming message **cancels and reschedules** two asyncio timers per chat: a reply timer (`REPLY_DELAY = 7s` after the last message) and a flush timer (`CHAT_BLACKOUT_TIME = 60s` of silence = "conversation over"). This debounce means that if you fire off five texts in a row, Rachel reads all five and answers once — the way a human who was mid-typing would. Replies are then split on blank lines into separate Telegram messages and sent with a simulated typing delay, so a long answer arrives as a believable burst of texts rather than one instant wall. The loop is also race-hardened: the LLM call is `asyncio.shield`-ed so a racing new message can't cancel an in-flight response, a per-chat reply lock stops a queued reply from reading context before the previous one finishes sending, and Rachel's own reply is inserted back into the buffer **at its send-order position by message id** — messages that arrived during a slow multi-message send already sit at the tail.

### Cheap-gate → LLM-router reply suppression
Sitting in a busy group chat without being annoying is a genuinely hard product problem. Rachel solves it in two tiers. First a **no-LLM `checker_node`**: if the message is a 1-on-1 DM or Rachel was @-mentioned/replied-to, she's definitely meant to respond — skip straight to generating. Otherwise a lightweight **`router_node`** makes an LLM judgement call on whether a reply is even warranted (it fails *open* on LLM errors), and short-circuits the whole graph to `END` if not — no summary, no context retrieval, no response, no cost. She still *reads and remembers* every message either way — she just doesn't talk over the room. A buffer cap (`MAX_BUFFER_LEN = 150`) is enforced on every incoming message, so a busy group she never replies in still flushes and feeds the memory pipelines instead of growing unbounded.

### An agentic context fetcher that runs in parallel with the summarizer
Instead of stuffing every prompt with Rachel's full schedule, world knowledge, and everyone's profile, a dedicated **`context_fetcher_node`** — the only node in the reply graph with tool-calling — decides *per message* what's worth looking up. It has the calendar tools, a `search_world_view` tool over the knowledge graph, and a unified `search_user_info(user_id, query)` tool that fetches both a participant's free-form fact memories and their structured profile in one call. It's a deliberate **single pass** (one tool-selection call, all tools run once, outputs routed into typed state slots — no agent loop), runs **in parallel with the summarizer** so retrieval adds no latency to the critical path, and is wrapped in a 30-second timeout that **fails open to empty context** — memory can enrich a reply but can never stall or break one.

### Divider partitioning: anti-duplication without deleting context
Because the history slice includes Rachel's own past replies, she'd sometimes re-answer messages she'd already handled. The fix is a *divider*: each reply-graph node inserts one synthetic message into the transcript at the point just past Rachel's last reply — "everything above is context you already responded to; act on what's below." Nothing is ever removed, so the model keeps full conversational context, but the actionable region is explicit. The memory pipelines use the same insert-a-divider-remove-nothing shape but partition on a **persisted per-chat watermark** instead (see below) — two deliberately separate mechanisms for two different notions of "already handled."

### Temporal knowledge-graph memory (Graphiti on Neo4j)
When a conversation finishes, two memory pipelines extract durable facts — *general* world facts and *personal* per-user facts — and ingest each one as an episode into **Graphiti**, a temporal knowledge graph on Neo4j. This replaces an earlier design where flat stores (a markdown file, per-user text rows) were re-read and re-written wholesale by an LLM "consolidation" pass on every update. With a graph, **deduplication is structural** (entities resolve to the same node; conflicting edges are reconciled temporally, newer facts superseding older ones) and **retrieval is selective** — the context fetcher runs a hybrid semantic + BM25 + graph search (reciprocal rank fusion) scoped to the relevant partition, pulling only the subgraph that touches the current conversation instead of dumping the whole store into every prompt. World facts live under one `worldview` partition; each user's personal memories live under their own `user-facts-<id>` partition — one graph, cleanly namespaced by Graphiti group ids. Retrieval harvests relationship **edges** and verbatim ingested **episodes**, deliberately dropping Graphiti's node summaries (a lossy, truncated restatement of the same facts).

### A persisted memory watermark so nothing is extracted twice
Chat context is re-seeded from the DB whenever a buffer is flushed, so naive extraction would re-mine the same old messages every time a conversation ends. Each chat keeps a persisted high-water mark (`last_processed_message_id`) of the newest message already handed to the extractors. The extraction transcript is divided at the watermark ("only extract from below this line"), all extractor prompts are told about the divider, and the watermark advances only after the pipelines are launched — so facts are mined from each message exactly once across restarts, re-seeds, and buffer flushes.

### One-call-lagged mood system
A `summarizer_node` reads the conversation and classifies its emotional register into one of seven moods (`default`, `formal`, `sad_frustration`, `excited_happy`, `casual_rant`, `drama_sharing`, `flirt`). That mood is stored per-chat and injected into the **next** call's responder, which picks a matching set of tone exemplars from `CONVERSATION_STYLE`. The one-message lag is deliberate: it lets the responder run without waiting on the summarizer, and mirrors how a person's tone catches up to the vibe of a conversation a beat late rather than instantly.

### Hybrid memory writes: graph ingestion where facts accumulate, deterministic merge where they don't
Each user has two kinds of memory: free-form **facts** (open-ended, accumulating — "her sister has an exam next week") and a fixed-slot **profile** of 16 standing attributes (life stage, food vibe, sense of humour, …) stored as one Postgres JSONB blob. Facts go through Graphiti, which earns its LLM cost by doing real entity resolution and temporal conflict handling. Profile slots are single standing values, so they're merged by **deterministic code-level field overwrite** — newer non-empty value wins, no LLM call, guarded by per-user locks against concurrent conversation finalizations. The profile extractor is even shown each user's *current* profile as context so it skips already-known slots. Using a model only where the merge is genuinely ambiguous keeps the pipeline cheap and predictable.

### Schema-as-single-source-of-truth structured output
The profile slot list (`USER_PROFILE_FIELDS`) and the mood list (`MOOD_LABELS`) are each defined once in code. The LLM's structured-output Pydantic models are built **dynamically** from those lists (`pydantic.create_model`, JSON-schema enums), and the same lists drive prompt rendering, the responder's profile display, and the admin dashboard (which fetches the slot schema over REST rather than hardcoding it). Adding a profile slot or a mood needs no database migration — and the model can never emit a value the rest of the system doesn't understand. The same principle covers tools: `CALENDAR_TOOLS` both binds the tools to the LLM *and* renders their descriptions into the context fetcher's prompt, so adding a tool is a one-line change.

### Making Graphiti reliable over OpenRouter
Graphiti assumes a provider with native constrained decoding; OpenRouter silently downgrades `json_schema` mode to a plain JSON object for models without it, so field names stop being enforced and Graphiti's internal validation fails. Two load-bearing workarounds fix this: the LLM client runs in `json_object` mode (which embeds the schema, field names included, into the prompt), and a `_RetryingOpenAIGenericClient` subclass re-rolls the call with a corrective note whenever the model returns valid JSON whose *shape* doesn't match the expected response model — a failure class Graphiti's built-in transport-level retries never see. Small, unglamorous, and the difference between a demo and a system that ingests memory reliably on a budget model.

### Personality as tunable sliders + a lived-in schedule
Rachel's character isn't a frozen prompt. Twelve personality traits (Extraversion, Neuroticism, Humor/Irony, Patience, …) are stored as `low`/`medium`/`high` sliders, each with its own prompt fragment, assembled live into the responder prompt and tunable at runtime over Telegram, REST, or the dashboard. Trait *definitions* re-seed from code on every startup while admin-tuned levels survive. A seeded **weekly schedule** (rows keyed by day + start hour, with durations that span midnight) gives her a current activity and day overview, surfaced to the LLM through the calendar tools — so "what are you up to?" gets an answer consistent with the time of day.

## Architecture

```mermaid
flowchart TB
    subgraph TG[Telegram]
        U[Users / group chats]
        A[Admin]
    end

    subgraph APP[FastAPI app · single asyncio loop]
        RC[Rachel client<br/>Telethon · anon.session]
        BOT[Admin bot<br/>Telethon · bot.session]
        API[Admin HTTP API + dashboard<br/>single-file SPA at /]
        BUF[(Per-chat in-memory<br/>message buffers + timers)]
    end

    subgraph LLM[LangGraph pipelines · OpenRouter]
        REPLY[reply graph:<br/>checker → router →<br/>summarizer ∥ context_fetcher → responder]
        WV[worldview:<br/>extract → ingest]
        UF[userfacts:<br/>facts + profile branches]
    end

    DB[(Postgres<br/>history · summaries · profiles<br/>traits · schedule · watermarks)]
    KG[(Neo4j · Graphiti<br/>worldview + per-user<br/>knowledge graph)]

    U -->|messages| RC
    RC -->|buffer + debounce| BUF
    BUF -->|reply timer fires| REPLY
    REPLY -->|typed, split reply| U
    BUF -->|conversation ends / buffer full| WV
    BUF -->|conversation ends / buffer full| UF
    REPLY <-->|summary · profile reads| DB
    REPLY <-->|hybrid search| KG
    WV -->|episodes| KG
    UF -->|episodes| KG
    UF -->|profile JSONB| DB
    BUF <-->|seed / flush| DB
    A <-->|commands| BOT
    A <-->|REST / dashboard| API
    BOT <--> DB
    API <--> DB
```

- **Rachel client** (`app/telegram/client.py`) — the account your friends talk to. Owns the per-chat message buffers and the reply/flush timers; seeds context from the DB on first contact and flushes back on conversation end.
- **Admin bot** (`app/telegram/bot.py`) — a second Telegram bot only you can talk to, for inspecting and tuning Rachel's prompts, traits, memory, and history live.
- **Admin HTTP API + dashboard** (`app/routers/admin.py`, `app/static/index.html`) — the same controls over REST, plus a self-contained single-file dashboard SPA (vanilla JS, no build step) served at `/`, designed to sit behind nginx basic auth.
- **LLM service** (`app/services/llm.py`) — the reply pipeline: gating, mood detection, agentic context retrieval, and response generation as one compiled LangGraph graph.
- **Memory services** (`app/services/worldview.py`, `app/services/userfacts.py`, `app/services/memory.py`) — the post-conversation memory pipelines, orchestrated by a single `update_memories` entry point.
- **Graphiti layer** (`app/services/graphiti.py`) — the shared knowledge-graph client, episode ingestion, hybrid search, and the OpenRouter compatibility workarounds.
- **Data layer** (`app/models.py`, `app/repository.py`, `app/database.py`) — async SQLAlchemy over Postgres, with hot reads cached in module globals.

### The reply pipeline

```mermaid
flowchart LR
    START((start)) --> CHK[checker_node<br/>cheap no-LLM gate]
    CHK -->|must reply| SUM[summarizer_node<br/>detect mood + summary]
    CHK -->|must reply| CTX[context_fetcher_node<br/>tools: calendar · world view · user info]
    CHK -->|maybe| RT[router_node<br/>reply warranted?]
    RT -->|no| E((end))
    RT -->|yes| SUM
    RT -->|yes| CTX
    SUM --> RESP[responder_node<br/>generate reply]
    CTX --> RESP
    RESP --> E
```

Once the gate passes, `summarizer_node` and `context_fetcher_node` run **in parallel** and the responder joins them. `responder_node` injects the fresh summary, personality traits, conversation mood, formatted date/time, and whatever the context fetcher retrieved — schedule context, world-view search results, and per-participant facts + fixed-slot profiles — and returns the reply plus a one-sentence `reason` that's persisted with the message for traceability.

The compiled LangGraph graph, rendered:

![Reply pipeline graph](graph_llm.png)

### The memory pipelines

When a conversation ends (or the buffer fills), `update_memories` divides the transcript **once** at the chat's watermark, then runs both pipelines concurrently. Both short-circuit when nothing new has arrived since the watermark, and both never raise — memory upkeep can't crash the message loop.

**Worldview** — extract durable *general* facts from below the divider, then ingest each as a Graphiti episode into the `worldview` partition (Graphiti handles dedup and temporal conflict resolution on ingest, so there is no hand-rolled consolidation step):

![Worldview pipeline graph](graph_worldview.png)

**User-facts** — two branches fan out from `START` in parallel: a free-form facts branch (extract per-sender → ingest into that user's Graphiti partition) and a structured-profile branch (extract + deterministic locked write in one node). Extractors see only sender *names* — less hallucination-prone than numeric ids — which are resolved back to ids in code, with unknown names dropped:

![User-facts pipeline graph](graph_userfacts.png)

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | FastAPI + Uvicorn | Async HTTP app and lifespan that hosts both Telegram clients |
| Messaging | Telethon (MTProto) | Two persistent Telegram client connections — no webhook |
| LLM orchestration | LangGraph + langchain-openrouter | Stateful multi-node graphs for reply, worldview, and user-facts |
| LLM provider | OpenRouter (default `deepseek/deepseek-v4-flash`) | Model-agnostic inference via an OpenAI-compatible API; separate billable key for memory ingestion |
| Long-term memory | Graphiti + Neo4j 5 | Temporal knowledge graph: entity resolution, temporal edge invalidation, hybrid RRF search |
| Database | PostgreSQL 16 (async SQLAlchemy 2.0 + asyncpg) | Conversation history, summaries, users, traits, schedule, JSONB profiles, memory watermarks |
| Migrations | Alembic | Versioned schema management |
| Admin UI | Single-file vanilla-JS SPA | Zero-build dashboard served by FastAPI, auth delegated to nginx |
| Config | pydantic-settings | `.env`-driven, cached settings |
| Tooling | uv | Dependency management and runner |
| Infrastructure | Docker Compose | Local Postgres + Neo4j (and optional containerized app) |

## Features

- Human-like texting: debounced replies, simulated typing speed, multi-message bursts, race-safe send ordering
- Group-chat-aware: reads everything, only speaks when DM'd, @-mentioned, or replied to — and a router can still decline to reply
- Never re-answers a message she already handled (divider partitioning), never re-extracts memory from old messages (persisted watermark)
- Seven conversational moods with matching tone exemplars, applied with a deliberate one-turn lag
- Persistent two-tier memory in a temporal knowledge graph: general world facts + per-person facts, plus a 16-slot structured profile in Postgres
- Selective recall: an agentic context fetcher searches the graph and schedule per message instead of dumping all memory into every prompt
- Self-maintaining memory with structural dedup and newer-fact-wins conflict resolution, run automatically when a conversation ends
- Twelve runtime-tunable personality traits (low/medium/high sliders)
- A seeded weekly schedule that gives Rachel a believable "current activity", exposed to the LLM as tools
- Admin control plane over Telegram, REST, **and** a web dashboard: prompts, traits, memory, profiles, history, summaries
- Bulk world-knowledge seeding scripts (single fact or whole markdown files)
- Every reply stores a one-sentence `reason` for debugging and traceability
- Buffers flushed to Postgres on shutdown and on conversation end, surviving restarts

## Future Potential

The next frontier is richer memory *generation*, splitting what Rachel remembers into the two kinds of long-term memory humans have. **Temporal (episodic) memories** capture events as they happen — "Sarah's job interview is on Thursday" — with lifecycles: they're born mid-conversation rather than only at conversation end, they expire or get invalidated as reality moves on (the interview happened; the plan was cancelled), and Graphiti's temporal edge invalidation is the natural substrate for tracking that decay. **Semantic memories** are the timeless residue distilled from those episodes — "Sarah works in finance" — which is what the current extractors approximate today. Layered on top is an **evolving user impressions** feature: rather than just accumulating facts about a person, Rachel forms an *opinion* of them — warm, guarded, amused, worried — that drifts gradually as interactions accumulate, the way real impressions do, and colours her tone with each person independently of the conversation's mood.

The architecture generalizes cleanly beyond one character. The persona is data — system prompts, trait sliders, schedule, moods — not code, so the same engine could host a roster of distinct characters, or be offered as a "believable NPC" backend for game studios and interactive-fiction platforms. Swapping Telethon for the Discord or WhatsApp Business APIs is a client-layer change, not an architectural one, since the reply/memory pipelines are transport-agnostic. OpenRouter already makes the underlying model a config value, so cost/quality can be tuned per deployment without touching the graphs.

There's also a clear research and tooling angle: the structured per-user profiles, the temporal fact graph, and the stored `reason` on every reply make this a natural testbed for studying long-horizon persona consistency, memory drift, and tone steering — the kind of evaluation harness that companionship, education, and customer-experience products increasingly need.

---

## Getting Started

### Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | ≥ 3.11 | [python.org](https://www.python.org/downloads/) (uv can also install it) |
| uv | latest | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Docker Desktop | latest | [docker.com](https://www.docker.com/products/docker-desktop/) (for Postgres and Neo4j) |

Docker Desktop must be running before you start the databases. You can verify by running `docker ps` — if it returns a list (even an empty one) instead of an error, Docker is running. You will also need two Telegram bots and a Telegram developer app (details in step 4).

### 1. Clone the repository

```bash
git clone https://github.com/GeneralR3d/Rachel.git
cd Rachel
```

*(Navigate into the project folder — this is required for all following commands.)*

### 2. Install dependencies

**Python libraries the app needs** (FastAPI, Telethon, LangGraph, Graphiti, SQLAlchemy, …):
```bash
uv sync
```
*uv reads `pyproject.toml`/`uv.lock`, creates a virtual environment, and installs everything. You should see it resolve and install the dependency set.*

### 3. Start infrastructure

Here "infrastructure" means two containers: **PostgreSQL** (conversation state) and **Neo4j** (the Graphiti knowledge graph). Compose runs both with the credentials the app expects:
```bash
docker compose up -d db neo4j
```
*You should see `Container rachel-db Started` and `Container rachel-neo4j Started`. If you get an error about Docker not running, open Docker Desktop and wait for it to finish starting, then re-run. Postgres is exposed on host port **5433** (mapped to internal 5432); Neo4j exposes bolt on **7687** and its browser UI on **7474** — both bound to loopback only. You can inspect the graph anytime at `http://localhost:7474`.*

### 4. Configure environment variables

Copy the example file:
```bash
cp template.env .env
```

Now open `.env` in a text editor and fill in the following values.

#### Telegram

**`TELEGRAM_API_ID`** / **`TELEGRAM_API_HASH`** *(required)*
What they do: identify *your developer app* to Telegram's MTProto API (used by both clients).
Where to get them: log in at [my.telegram.org](https://my.telegram.org) → **API development tools** → create an app → copy the `api_id` and `api_hash`.
Example: `TELEGRAM_API_ID=1234567` / `TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef`

**`TELEGRAM_BOT_TOKEN`** *(required)*
What it does: logs in the separate **admin** bot — the one only you talk to.
Where to get it: message [@BotFather](https://t.me/botfather) → `/newbot` → copy the token it gives you.
Example: `TELEGRAM_BOT_TOKEN=123456:ABC-DEF...`

> Rachel's *own* credentials are **not** stored in `.env`. They're entered once, interactively, in step 6.

**`ADMIN_ID`** *(required)*
What it does: the only Telegram user ID allowed to issue admin commands.
Where to get it: message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.
Example: `ADMIN_ID=987654321`

#### LLM

**`OPENROUTER_API_KEY`** *(required)*
What it does: authenticates Rachel's own model calls (router, summarizer, context fetcher, responder).
Where to get it: sign in at [openrouter.ai](https://openrouter.ai/keys) → **Keys** → create a key.
Example: `OPENROUTER_API_KEY=sk-or-v1-...`

**`OPENROUTER_GRAPHITI_API_KEY`** *(optional)*
What it does: a **separate** key billed for everything memory-related — the fact extractors and all of Graphiti's internal LLM, embedding, and reranker calls (memory ingestion is several LLM round-trips per fact, so it's useful to meter it separately). Leave blank to reuse `OPENROUTER_API_KEY`.

**`OPENROUTER_MODEL`** *(optional)*
What it does: which model to route requests to.
Default: `deepseek/deepseek-v4-flash`. Any OpenRouter model slug works.

**`OPENROUTER_EMBEDDING_MODEL`** *(optional)*
What it does: the embedding model Graphiti uses for semantic search over the knowledge graph, also routed through OpenRouter (no separate embeddings provider or key needed).
Default: `openai/text-embedding-3-small`.

#### Databases & naming

**`DB_PASSWORD`** *(optional, default `rachel`)*
What it does: the dockerized Postgres password. Compose reads it for `POSTGRES_PASSWORD` and `DATABASE_URL` interpolates it, so it lives in exactly one place. Keep it above `DATABASE_URL` in the file (dotenv resolves top-down).

**`DATABASE_URL`** *(required)*
What it does: async SQLAlchemy connection string.
Value: use `postgresql+asyncpg://rachel:${DB_PASSWORD}@localhost:5433/rachel` when running the app locally against the Dockerized DB (note port **5433**). If you run the app *inside* Docker Compose instead, use host `db` and port `5432` (kept literal — Compose env injection doesn't expand `${...}`).

**`NEO4J_PASSWORD`** *(required)* / **`NEO4J_USER`** *(default `neo4j`)*
What they do: credentials for the Neo4j container (Compose sets `NEO4J_AUTH` from the same value, so app and DB always agree).

**`NEO4J_URI`** *(required)*
What it does: bolt connection string for Graphiti.
Value: `bolt://localhost:7687` locally; `bolt://neo4j:7687` when the app runs inside Compose.

**`BOT_NAME`** *(optional)* — display name used in summaries/labelling. Default: `Rachel`.
**`USER_NAME`** *(optional)* — your name, used for labelling. Default: unset.

### 5. Initialize the database

```bash
uv run alembic upgrade head
```
*This creates all the Postgres tables the app needs. You only need to run it once (and again whenever you pull new migrations). Neo4j needs no schema setup — Graphiti builds its indices on first use.*

### 6. Log Rachel in (one time)

Uvicorn runs non-interactively, so Rachel's session must be created first. This writes `anon.session`:
```bash
uv run python -m scripts.login
```
*Follow the interactive prompts to log in the account Rachel will speak as.*

### 7. Run the app

```bash
uv run uvicorn app.main:app --reload
```
*On startup the app seeds the system prompts, personality traits, and weekly schedule into Postgres, starts both Telegram clients, and serves the API at `http://localhost:8000`. You should see "Telethon clients started." in the logs.* The admin dashboard is at `http://localhost:8000/` and interactive API docs at `http://localhost:8000/docs`.

### Verify everything is working

```bash
curl http://localhost:8000/health
```
Expected response:
```json
{ "status": "ok" }
```
Then message Rachel from another Telegram account — after a few seconds' pause she should reply, typing it out in real time.

### (Optional) Seed Rachel's world knowledge

Give her things to "know" before anyone talks to her (Neo4j must be up; each fact is several LLM round-trips, so bulk ingestion takes a while):

```bash
uv run python -m scripts.add_worldview_fact "Chagee is a bubble tea brand"   # one fact
uv run python -m scripts.ingest_worldview_md path/to/facts.md                # a whole markdown file
uv run python -m scripts.clear_graph                                         # WIPE the graph (typed confirmation)
```

---

## Admin controls

Three interfaces expose the same state: the Telegram admin bot, the REST API, and the web dashboard at `/` (a single-file SPA meant to sit behind nginx basic auth in production — it has no auth of its own).

Over Telegram (only `ADMIN_ID` is honoured):

| Command | Description |
|---|---|
| `/get_responder_system_prompt` · `/set_responder_system_prompt <text>` | View / set Rachel's main persona prompt |
| `/get_summarizer_system_prompt` · `/set_summarizer_system_prompt <text>` | View / set the summarizer prompt |
| `/list_user_names` · `/list_chats` | Enumerate known users / chats |
| `/get_history <chat_id>` · `/clear_history <chat_id>` | Inspect / clear a chat's stored messages (incl. `reason`) |
| `/get_summary <chat_id>` · `/delete_summary <chat_id>` | Inspect / delete a chat's running summary |
| `/list_traits` · `/set_trait <id> <low\|medium\|high>` · `/reset_traits` | Tune personality sliders |
| `/get_user_facts <user_id>` | Dump every fact episode in a user's knowledge-graph partition |
| `/add_user_facts <user_id> <fact>` | Ingest a new fact episode (slow — several LLM round-trips; the bot acks first) |
| `/get_user_profile <user_id>` · `/delete_user_profile <user_id>` | Inspect / delete a user's structured profile slots |

There is deliberately no edit/delete for individual facts: Graphiti's dedup and temporal conflict resolution supersede old facts on ingest — to "correct" a fact, ingest the corrected version.

Over REST (`app/routers/admin.py`): `GET/PUT /responder-system-prompt`, `GET/PUT /summarizer-system-prompt`, `GET /users/names`, `GET /list-chats`, `GET/DELETE /history/{chat_id}`, `GET/DELETE /summary/{chat_id}`, `GET/PUT/DELETE /user-profile/{user_id}`, `GET/POST /user-facts/{user_id}`, `GET /user-profile-fields`, `GET /personality`, `PATCH /personality/{trait_id}`, `POST /personality/reset`, `GET /health`.

To scope the admin bot's command menu to just your chat, use the Bot API directly instead of BotFather — send a `setMyCommands` request with the scope field:

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
        {"command": "list_user_names", "description": "List all usernames and names and telegram_user_id"},
        {"command": "get_user_facts", "description": "Dump all fact episodes for a user: /get_user_facts <user_id>"},
        {"command": "add_user_facts", "description": "Ingest a new fact for a user: /add_user_facts <user_id> <fact text>"},
        {"command": "get_user_profile", "description": "Get the structured profile for a user: /get_user_profile <user_id>"},
        {"command": "delete_user_profile", "description": "Delete the structured profile for a user: /delete_user_profile <user_id>"},
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

## Development

```bash
uv run uvicorn app.main:app --reload      # run with hot reload
uv run alembic upgrade head               # apply all migrations
uv run alembic downgrade -1               # roll back one migration
uv run alembic revision -m "msg"          # generate a new migration after a schema change
uv run python -m scripts.draw_graphs      # render the LangGraph pipelines to PNG
docker compose up -d db neo4j             # start just Postgres + Neo4j
```

There is no test suite or lint config in this repo yet. The original Telethon/SQLite implementation is preserved under [`Reference/`](Reference/) as the porting reference, and deployment notes live in [`DEPLOY.md`](DEPLOY.md) and [`DEPLOY_DOCKER.md`](DEPLOY_DOCKER.md).

> **Known gap:** there's a race in `_flush_chat` (`app/telegram/client.py`) where a message arriving between the flush write and the buffer clear can be dropped. It's marked with a `#TODO` in the source.
