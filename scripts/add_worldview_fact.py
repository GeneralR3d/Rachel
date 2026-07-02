"""Helper: add single world-view facts to Graphiti by hand.

Useful for testing ingestion / seeding a fact without going through a full
Telegram conversation. Each fact is ingested as one episode, via the same
``worldview.ingest_node`` path the live pipeline uses (same naming / SGT
reference_time / lock, and Graphiti's own entity+edge extraction and dedup
run on each add).

Two ways to run, from the repo root (Neo4j must be up):

    # One-shot: pass fact(s) as arguments (quote each). Ingests and exits —
    # works even when stdin isn't an interactive terminal.
    uv run python -m scripts.add_worldview_fact "Chagee is a bubble tea brand"

    # Interactive: no arguments → prompt for one fact per line. Requires a real
    # terminal (a piped/closed stdin exits immediately at the first EOF).
    uv run python -m scripts.add_worldview_fact
"""

import asyncio
import sys
from pathlib import Path

# Allow running as a plain file (`python scripts/add_worldview_fact.py`), not
# just as a module (`python -m scripts.add_worldview_fact`): put the repo root
# on sys.path so `app` imports resolve regardless of invocation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import graphiti as graphiti_service
from app.services import worldview


async def ingest_one(fact: str) -> None:
    state = {"conversation": [], "extracted_facts": [fact], "chat_id": None}
    await worldview.ingest_node(state)


async def run_args(facts: list[str]) -> None:
    """One-shot mode: ingest each CLI-provided fact, then exit."""
    await graphiti_service.get_graphiti()
    for fact in facts:
        fact = fact.strip()
        if not fact:
            continue
        try:
            await ingest_one(fact)
            print(f"  ✓ ingested: {fact}")
        except Exception as e:
            print(f"  ✗ failed: {fact}\n    {e}")


async def run_interactive() -> None:
    """Prompt for one fact per line until blank/quit/EOF."""
    await graphiti_service.get_graphiti()
    print("Enter a world-view fact per line. Blank line, 'quit', or Ctrl-D to exit.\n")
    while True:
        try:
            fact = (await asyncio.to_thread(input, "fact> ")).strip()
        except EOFError:
            print()
            break
        if not fact or fact.lower() in {"quit", "exit"}:
            break
        try:
            await ingest_one(fact)
            print(f"  ✓ ingested: {fact}")
        except Exception as e:  # keep the loop alive on a bad ingest
            print(f"  ✗ failed: {e}")


async def main() -> None:
    args = sys.argv[1:]
    try:
        if args:
            await run_args(args)
        else:
            await run_interactive()
    finally:
        g = await graphiti_service.get_graphiti()
        await g.close()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
