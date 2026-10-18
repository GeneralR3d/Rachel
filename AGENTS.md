# Repository Guidelines

## Project Structure & Module Organization

Rachel is a Python 3.11+ Telegram persona service built with FastAPI, Telethon, and LangGraph.

- `app/main.py` manages startup and shutdown; `app/telegram/` contains the conversation client and admin bot.
- `app/services/` implements reply generation, context retrieval, memory, Graphiti integration, metrics, and tracing.
- `app/models.py`, `app/repository.py`, and `app/database.py` define persistence; `alembic/versions/` contains schema migrations.
- `app/routers/admin.py` serves admin APIs; `app/static/index.html` is the dashboard, with no frontend build step.
- `tests/` contains automated tests; `scripts/` holds login, ingestion, and graph-rendering utilities. Root `graph_*.png` files document workflows; `docs/superpowers/` stores designs and plans.

## Build, Test, and Development Commands

Run commands from the repository root after copying `template.env` to `.env` and configuring credentials.

- `uv sync --frozen`: install dependencies from `uv.lock`.
- `docker compose up -d db neo4j`: start backing services. For local execution, use PostgreSQL port `5433` and Neo4j Bolt port `7687`.
- `uv run alembic upgrade head`: apply database migrations.
- `uv run python -m scripts.login`: create the Telegram account session interactively.
- `uv run uvicorn app.main:app --reload`: run the application locally.
- `uv run python -m unittest discover -s tests -v`: run the test suite.
- `docker compose build app`: build the application container.
- `uv run python -m scripts.draw_graphs`: regenerate workflow diagrams after graph changes.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and modules, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Follow adjacent type annotations and import conventions. Keep database access in the repository layer and network operations asynchronous. No formatter or linter is configured; avoid unrelated formatting changes.

## Testing Guidelines

Use standard-library `unittest`, including `IsolatedAsyncioTestCase` for async behavior. Name files `test_*.py` and methods `test_<behavior>`. Mock model and HTTP calls with `AsyncMock`, `patch`, or `httpx.MockTransport`. Cover routing, retrieval limits, failure fallbacks, and tracing when changing those workflows. No coverage threshold is configured. Tests import application settings, so supply valid development configuration.

## Commit & Pull Request Guidelines

Follow existing `feat:`, `fix:`, and `chore:` commit prefixes with a concise description. Keep changes focused. PRs should explain behavior changes, link relevant issues, report validation, and note migrations or configuration changes. Include screenshots for dashboard changes. Update `uv.lock` alongside dependency changes.

## Security & Configuration

Never commit `.env`, Telegram `*.session` files, or credentials. Keep the admin dashboard behind authenticated access; it has no application-level authentication. Use disposable data when exercising destructive graph utilities.
