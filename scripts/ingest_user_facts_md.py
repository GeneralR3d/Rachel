"""Bulk-ingest per-user facts from a markdown file into Graphiti.

The counterpart to ``scripts.ingest_worldview_md`` for the per-user facts
pipeline. Given a user id, it reads ``user_fact_<user_id>.md`` (one fact per
``- `` bullet) and ingests each bullet as an episode into that user's Graphiti
partition via ``userfacts.add_user_facts`` — the same naming / lock / dedup
semantics as the live pipeline and the admin surfaces.

Run from the repo root (Neo4j must be up):
    uv run python -m scripts.ingest_user_facts_md 12345

Caution: this is additive. Re-running creates fresh episodes for the same
facts (Graphiti dedups entities/edges, but not raw episodes), so only run it
once unless you intend to re-ingest.
"""

import asyncio
import sys
from pathlib import Path

# Allow running as a plain file, not just as a module: put the repo root on
# sys.path so `app` imports resolve regardless of invocation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import graphiti as graphiti_service
from app.services import userfacts


def parse_facts(text: str) -> list[str]:
    """Return one fact per ``- `` bullet, ignoring headings/blank lines."""
    facts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            facts.append(line[2:].strip())
    return [f for f in facts if f]


async def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: uv run python -m scripts.ingest_user_facts_md <user_id>")
        sys.exit(1)
    try:
        user_id = int(sys.argv[1])
    except ValueError:
        print(f"Invalid user_id {sys.argv[1]!r}: must be an integer.")
        sys.exit(1)

    md_path = Path(f"user_fact_{user_id}.md")
    if not md_path.exists():
        print(f"{md_path} not found (run from the repo root); nothing to ingest.")
        return
    facts = parse_facts(md_path.read_text(encoding="utf-8"))
    print(f"Parsed {len(facts)} fact(s) from {md_path} for user {user_id}")
    if not facts:
        return
    # One add_episode per fact, sequential (slow — several LLM round-trips each).
    await userfacts.add_user_facts(user_id, facts)
    g = await graphiti_service.get_graphiti()
    await g.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
