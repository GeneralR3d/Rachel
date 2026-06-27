# Rachel

> A Telegram chatbot that texts like a real person — a 22-year-old Singaporean university student with moods, memory, a weekly schedule, and a tunable personality — not like a chatbot.

![Python](https://img.shields.io/badge/python-%E2%89%A53.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688)
![Postgres](https://img.shields.io/badge/Postgres-16-336791)
![LangGraph](https://img.shields.io/badge/LangGraph-stateful%20agents-orange)
![OpenRouter](https://img.shields.io/badge/LLM-OpenRouter-black)

## Who is Rachel?

Most chatbots forget who you are between conversations, and answer every single message whether or not it was meant for them. Drop one into a group chat and it becomes a nuisance within minutes.

Rachel is the opposite. She's a conversational persona — "Rachel," a 22-year-old marketing student at NTU in Singapore, with a backstory, insecurities, a church youth group, a bubble-tea order, and a weekly timetable. She buffers incoming messages and waits a beat before replying, types at a human speed, splits longer thoughts across several messages, and only chimes in when she's actually being addressed. Over time she builds up a memory of the world and of each person she talks to, and her tone shifts with the emotional read of the conversation.

Technically, Rachel is a FastAPI service that runs two Telethon (Telegram) clients on the same asyncio event loop, backed by Postgres, with all the language work driven by a set of **LangGraph** state machines over **OpenRouter**. The interesting part isn't that it calls an LLM — it's the machinery around the LLM that makes the output feel like a person texting you back. The longer-term vision is a persona engine: a reusable substrate for believable, stateful AI characters that live inside the messaging apps people already use.

## Key Innovations

### Human-cadence messaging loop
Rachel never replies on the raw message-arrival event. Each incoming message **cancels and reschedules** two asyncio timers per chat: a reply timer (`REPLY_DELAY = 7s` after the last message) and a flush timer (`CHAT_BLACKOUT_TIME = 60s` of silence = "conversation over"). This debounce means that if you fire off five texts in a row, Rachel reads all five and answers once — the way a human who was mid-typing would. Replies are then split on blank lines into separate Telegram messages and sent with a simulated typing delay (`TYPING_SPEED = 22` characters/second), so a long answer arrives as a believable burst of texts rather than one instant wall.

### Cheap-gate → LLM-router reply suppression
Sitting in a busy group chat without being annoying is a genuinely hard product problem. Rachel solves it in two tiers. First a **no-LLM `checker_node`**: if the message is a 1-on-1 DM or Rachel was @-mentioned/replied-to, she's definitely meant to respond — skip straight to generating. Otherwise a lightweight **`router_node`** makes an LLM judgement call on whether a reply is even warranted, and short-circuits the whole graph to `END` if not (no summary, no response, no cost). She still *reads and remembers* every message either way — she just doesn't talk over the room.

### One-call-lagged mood system
A `summarizer_node` reads the conversation and classifies its emotional register into one of seven moods (`default`, `formal`, `sad_frustration`, `excited_happy`, `casual_rant`, `drama_sharing`, `flirt`). That mood is stored per-chat and injected into the **next** call's responder, which picks a matching set of tone exemplars from `CONVERSATION_STYLE`. The one-message lag is deliberate: it lets the responder run without waiting on the summarizer, and mirrors how a person's tone catches up to the vibe of a conversation a beat late rather than instantly.

### Two-tier persistent memory (general world vs. per-person)
When a conversation finishes, two independent memory pipelines fire on the snapshotted dialogue. **Worldview** extracts durable, *general* facts about the world and stores them in a flat markdown file. **User-facts** keeps *personal* memory about each individual participant in Postgres. Both follow an **extract → consolidate** pattern where a consolidation LLM de-duplicates and resolves conflicts with newer information winning — so memory accretes cleanly instead of growing into contradictory sludge. The extractor short-circuits to `END` and skips all writes when it learns nothing new.

### Per-user fan-out with LangGraph `Send`
The user-facts consolidation step fans out via LangGraph's `Send` primitive — one parallel LLM instance per user who picked up new facts — each guarded by a per-user `asyncio.Lock` so concurrent conversation finalizations touching the same person can't clobber each other's read-modify-write.

### Hybrid memory writes: LLM where it helps, deterministic where it doesn't
Each user has two kinds of memory on one row: free-form `facts` (an open-ended bullet list) and a fixed-slot `profile` (16 standing attributes like life stage, food vibe, sense of humour). Free-form facts accumulate, so they get an **LLM consolidation pass**. Profile slots are single standing values, so they're merged by **deterministic code-level field overwrite** — no LLM call, newer non-empty value wins. Using a model only where the merge is genuinely ambiguous keeps the pipeline cheap and predictable.

### Schema-as-single-source-of-truth structured output
The profile slot list (`USER_PROFILE_FIELDS`) and the mood list (`MOOD_LABELS`) are each defined once in code. The LLM's structured-output Pydantic models are built **dynamically** from those lists (`pydantic.create_model`, JSON-schema enums), and the same lists drive prompt rendering and admin display. Adding a profile slot or a mood needs no database migration — the profile lives in a single JSONB blob — and the model can never emit a value the rest of the system doesn't understand.

### Personality as tunable sliders + a lived-in schedule
Rachel's character isn't a frozen prompt. Twelve personality traits (Extraversion, Neuroticism, Humor/Irony, Patience, …) are stored as `low`/`medium`/`high` sliders, each with its own prompt fragment, assembled live into the responder prompt and tunable at runtime over Telegram or HTTP. A seeded **weekly schedule** gives her a current activity and a day summary injected on every message, so "what are you up to?" gets an answer consistent with the time of day. Both are cached with short TTLs and invalidated on edit, since they're read on every single message.

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
        API[Admin HTTP API<br/>/system-prompt /personality /user-facts ...]
        BUF[(Per-chat in-memory<br/>message buffers + timers)]
    end

    subgraph LLM[LangGraph pipelines · OpenRouter]
        REPLY[checker → router →<br/>summarizer → responder]
        WV[worldview:<br/>extract → consolidate]
        UF[userfacts:<br/>facts + profile branches]
    end

    DB[(Postgres<br/>history · summaries · users<br/>traits · schedule · user_facts)]
    MD[worldview.md]

    U -->|messages| RC
    RC -->|buffer + debounce| BUF
    BUF -->|reply timer fires| REPLY
    REPLY -->|typed, split reply| U
    BUF -->|conversation ends / buffer full| WV
    BUF -->|conversation ends / buffer full| UF
    REPLY <--> DB
    WV <--> MD
    UF <--> DB
    BUF <-->|seed / flush| DB
    A <-->|commands| BOT
    A <-->|REST| API
    BOT <--> DB
    API <--> DB
```

- **Rachel client** (`app/telegram/client.py`) — the account your friends talk to. Owns the per-chat message buffers and the reply/flush timers; seeds context from the DB on first contact and flushes back on conversation end.
- **Admin bot** (`app/telegram/bot.py`) — a second Telegram bot only you can talk to, for inspecting and tuning Rachel's prompts, traits, memory, and history live.
- **Admin HTTP API** (`app/routers/admin.py`) — the same controls exposed over REST, with interactive docs at `/docs`.
- **LLM service** (`app/services/llm.py`) — the reply pipeline: gating, mood detection, and response generation as one compiled LangGraph graph.
- **Memory services** (`app/services/worldview.py`, `app/services/userfacts.py`) — the two post-conversation memory pipelines.
- **Data layer** (`app/models.py`, `app/repository.py`, `app/database.py`) — async SQLAlchemy over Postgres, with hot reads cached in module globals.

### The reply pipeline

```mermaid
flowchart LR
    START((start)) --> CHK[checker_node<br/>cheap no-LLM gate]
    CHK -->|must reply| SUM[summarizer_node<br/>detect mood + summary]
    CHK -->|maybe| RT[router_node<br/>reply warranted?]
    RT -->|no| E((end))
    RT -->|yes| SUM
    SUM --> RESP[responder_node<br/>generate reply]
    RESP --> E
```

`summarizer_node` runs before `responder_node` so its fresh summary is visible to the reply; the **mood** it detects is applied to the *next* turn's responder. `responder_node` injects the current summary, personality traits, conversation mood, formatted date/time, current scheduled activity, day summary, world view, and per-participant facts + profile into the prompt, and returns the reply plus a one-sentence `reason` that's persisted for traceability.

The compiled LangGraph graph, rendered:

![Reply pipeline graph](graph_llm.png)

### The memory pipelines

When a conversation ends (or the buffer fills), both memory pipelines fire on the snapshotted dialogue.

**Worldview** — extract general world facts, then consolidate them into the markdown store (skips straight to `END` when nothing new is learned):

![Worldview pipeline graph](graph_worldview.png)

**User-facts** — two branches fan out from `START` in parallel: a free-form facts branch (extract → per-user LLM consolidation via `Send`) and a structured-profile branch (extract + deterministic write in one node):

![User-facts pipeline graph](graph_userfacts.png)

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | FastAPI + Uvicorn | Async HTTP app and lifespan that hosts both Telegram clients |
| Messaging | Telethon (MTProto) | Two persistent Telegram client connections — no webhook |
| LLM orchestration | LangGraph + langchain-openrouter | Stateful multi-node graphs for reply, worldview, and user-facts |
| LLM provider | OpenRouter (default `google/gemini-2.0-flash-001`) | Model-agnostic inference via an OpenAI-compatible API |
| Database | PostgreSQL 16 (async SQLAlchemy 2.0 + asyncpg) | Conversation history, summaries, users, traits, schedule, per-user memory (incl. JSONB profiles) |
| Migrations | Alembic | Versioned schema management |
| Tokenization | tiktoken | Token counting for context budgeting |
| Config | pydantic-settings | `.env`-driven, cached settings |
| Tooling | uv | Dependency management and runner |
| Infrastructure | Docker Compose | Local Postgres (and optional containerized app) |

## Features

- Human-like texting: debounced replies, simulated typing speed, multi-message bursts
- Group-chat-aware: reads everything, only speaks when DM'd, @-mentioned, or replied to — and a router can still decline to reply
- Seven conversational moods with matching tone exemplars, applied with a deliberate one-turn lag
- Persistent two-tier memory: general world facts (markdown) + per-person facts & a 16-slot structured profile (Postgres)
- Self-maintaining memory: extract → consolidate with conflict resolution, run automatically when a conversation ends
- Twelve runtime-tunable personality traits (low/medium/high sliders)
- A seeded weekly schedule that gives Rachel a believable "current activity"
- Admin control plane over both Telegram **and** REST: prompts, traits, memory, history, summaries
- Every reply stores a one-sentence `reason` for debugging and traceability
- Buffers flushed to Postgres on shutdown and on conversation end, surviving restarts

## Future Potential

Near-term, the persona is the product surface: voice notes and image reactions (Telegram supports both), and proactive messages driven by the existing schedule (Rachel texts *you* when she's "free for bubble tea").

The biggest architectural leap is replacing the flat stores — the worldview markdown file and the per-user facts/profile rows — with a **knowledge graph**. Today both memory pipelines read and rewrite their entire store on every consolidation, and the responder loads *all* of it into the prompt on every message; this is simple and works at small scale, but it caps how much Rachel can remember before context bloat and consolidation cost become limiting. A graph of entities and relationships unlocks three things at once: **deduplication becomes structural** rather than relying on an LLM to spot that two bullet points describe the same fact (entities resolve to the same node, and conflicting edges are reconciled by recency at the node level); **querying becomes selective** — traverse from the people in the current chat to just the facts that touch this conversation, instead of scanning a whole file; and most importantly it enables **progressive disclosure**, loading a relevant subgraph into context on demand rather than dumping the entire worldview and every participant's full profile into every prompt. That keeps prompts small and focused as memory grows into the thousands of facts, and makes Rachel's recall feel pointed ("you mentioned your sister's exam") instead of indiscriminate.

The architecture generalizes cleanly beyond one character. The persona is data — system prompts, trait sliders, schedule, moods — not code, so the same engine could host a roster of distinct characters, or be offered as a "believable NPC" backend for game studios and interactive-fiction platforms. Swapping Telethon for the Discord or WhatsApp Business APIs is a client-layer change, not an architectural one, since the reply/memory pipelines are transport-agnostic. OpenRouter already makes the underlying model a config value, so cost/quality can be tuned per deployment without touching the graphs.

There's also a clear research and tooling angle: the structured per-user profiles and the stored `reason` on every reply make this a natural testbed for studying long-horizon persona consistency, memory drift, and tone steering — the kind of evaluation harness that companionship, education, and customer-experience products increasingly need.

---

## Getting Started

### Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | ≥ 3.11 | [python.org](https://www.python.org/downloads/) (uv can also install it) |
| uv | latest | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| Docker Desktop | latest | [docker.com](https://www.docker.com/products/docker-desktop/) (for Postgres) |

Docker Desktop must be running before you start the database. You can verify by running `docker ps` — if it returns a list (even an empty one) instead of an error, Docker is running. You will also need two Telegram bots and a Telegram developer app (details in step 4).

### 1. Clone the repository

```bash
git clone https://github.com/GeneralR3d/Rachel.git
cd Rachel
```

*(Navigate into the project folder — this is required for all following commands.)*

### 2. Install dependencies

**Python libraries the app needs** (FastAPI, Telethon, LangGraph, SQLAlchemy, …):
```bash
uv sync
```
*uv reads `pyproject.toml`/`uv.lock`, creates a virtual environment, and installs everything. You should see it resolve and install the dependency set.*

### 3. Start infrastructure

Here "infrastructure" means the PostgreSQL database that stores all conversation state. Compose runs it in a container with the credentials the app expects:
```bash
docker compose up -d db
```
*You should see `Container rachel-db Started`. If you get an error about Docker not running, open Docker Desktop and wait for it to finish starting, then re-run. The database is exposed on host port **5433** (mapped to Postgres's internal 5432).*

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

> Rachel's *own* bot token is **not** stored in `.env`. You'll create a second bot via @BotFather for Rachel and paste its token once, interactively, in step 6.

**`ADMIN_ID`** *(required)*
What it does: the only Telegram user ID allowed to issue admin commands.
Where to get it: message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.
Example: `ADMIN_ID=987654321`

#### LLM

**`OPENROUTER_API_KEY`** *(required)*
What it does: authenticates all model calls (reply, summarizer, memory pipelines).
Where to get it: sign in at [openrouter.ai](https://openrouter.ai/keys) → **Keys** → create a key.
Example: `OPENROUTER_API_KEY=sk-or-v1-...`

**`OPENROUTER_MODEL`** *(optional)*
What it does: which model to route requests to.
Default: `google/gemini-2.0-flash-001`. Any OpenRouter model slug works.

#### Database & naming

**`DATABASE_URL`** *(required)*
What it does: async SQLAlchemy connection string.
Value: use `postgresql+asyncpg://rachel:rachel@localhost:5433/rachel` when running the app locally against the Dockerized DB (note port **5433**). If you run the app *inside* Docker Compose instead, use host `db` and port `5432`.

**`BOT_NAME`** *(optional)* — display name used in summaries/labelling. Default: `Rachel`.
**`USER_NAME`** *(optional)* — your name, used for labelling. Default: unset.
**`WORLDVIEW_PATH`** *(optional)* — path to the markdown file holding Rachel's general memory. Default: `worldview.md`.

### 5. Initialize the database

```bash
uv run alembic upgrade head
```
*This creates all the tables the app needs. You only need to run it once (and again whenever you pull new migrations).*

### 6. Log Rachel in (one time)

Uvicorn runs non-interactively, so Rachel's session must be created first. This writes `anon.session`:
```bash
uv run python -m scripts.login
```
*When prompted, paste **Rachel's** bot token (the second bot you made with @BotFather) — not the admin token.*

### 7. Run the app

```bash
uv run uvicorn app.main:app --reload
```
*On startup the app seeds the system prompts, personality traits, and weekly schedule into Postgres, starts both Telegram clients, and serves the API at `http://localhost:8000`. You should see "Telethon clients started." in the logs.* Interactive API docs are at `http://localhost:8000/docs`.

### Verify everything is working

```bash
curl http://localhost:8000/health
```
Expected response:
```json
{ "status": "ok" }
```
Then message Rachel's bot from another Telegram account — after a few seconds' pause she should reply, typing it out in real time.

---

## Admin controls

Both interfaces expose the same state. Over Telegram (only `ADMIN_ID` is honoured):

| Command | Description |
|---|---|
| `/get_responder_system_prompt` · `/set_responder_system_prompt <text>` | View / set Rachel's main persona prompt |
| `/get_summarizer_system_prompt` · `/set_summarizer_system_prompt <text>` | View / set the summarizer prompt |
| `/list_user_names` · `/list_chats` | Enumerate known users / chats |
| `/get_history <chat_id>` · `/clear_history <chat_id>` | Inspect / clear a chat's stored messages (incl. `reason`) |
| `/get_summary <chat_id>` · `/delete_summary <chat_id>` | Inspect / delete a chat's running summary |
| `/list_traits` · `/set_trait <id> <low\|medium\|high>` · `/reset_traits` | Tune personality sliders |
| `/get_user_facts <user_id>` · `/set_user_facts <user_id> <text>` · `/delete_user_facts <user_id>` | Manage per-user free-form facts/preferences |
| `/get_user_profile <user_id>` · `/delete_user_profile <user_id>` | Inspect / delete a user's structured profile slots |

Over REST (`app/routers/admin.py`): `GET/PUT /system-prompt`, `GET /users/names`, `GET /list-chats`, `GET/DELETE /history/{chat_id}`, `GET/DELETE /summary/{chat_id}`, `GET /personality`, `PATCH /personality/{trait_id}`, `POST /personality/reset`, `GET/PUT/DELETE /user-facts/{user_id}`, `GET /health`.
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
        {"command": "list_user_names", "description": "List all usernames and names and telegram_user_id"},
        {"command": "get_user_facts", "description": "Get stored facts/preferences for a user: /get_user_facts <user_id>"},
        {"command": "set_user_facts", "description": "Set facts/preferences for a user: /set_user_facts <user_id> <facts text>"},
        {"command": "delete_user_facts", "description": "Delete stored facts/preferences for a user: /delete_user_facts <user_id>"},
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
docker compose up -d db                   # start just Postgres
```

There is no test suite or lint config in this repo yet. The original Telethon/SQLite implementation is preserved under [`Reference/`](Reference/) as the porting reference, and deployment notes live in [`DEPLOY.md`](DEPLOY.md).

> **Known gap:** there's a race in `_flush_chat` (`app/telegram/client.py`) where a message arriving between the flush write and the buffer clear can be dropped. It's marked with a `#TODO` in the source.
