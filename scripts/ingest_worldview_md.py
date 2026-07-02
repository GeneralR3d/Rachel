"""One-off backfill: ingest every bullet from worldview.md into Graphiti.

This is a migration helper for the move from the flat worldview.md file to the
Graphiti knowledge graph. It reuses ``worldview.ingest_node`` so each bullet
becomes an episode with the exact same naming / SGT reference_time / lock
semantics as the live pipeline.

Run once from the repo root (Neo4j must be up):
    uv run python -m scripts.ingest_worldview_md

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
from app.services import worldview

MD_PATH = Path("worldview.md")


def parse_facts(text: str) -> list[str]:
    """Return one fact per ``- `` bullet, ignoring headings/blank lines."""
    facts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            facts.append(line[2:].strip())
    return [f for f in facts if f]


async def main() -> None:
    if not MD_PATH.exists():
        print(f"{MD_PATH} not found (run from the repo root); nothing to ingest.")
        return
    facts = parse_facts(MD_PATH.read_text(encoding="utf-8"))
    print(f"Parsed {len(facts)} fact(s) from {MD_PATH}")
    if not facts:
        return
    # Feed the facts straight into the ingest node (skips the LLM extractor —
    # these are already curated facts), one add_episode per fact, sequential.
    state = {"conversation": [], "extracted_facts": facts, "chat_id": None}
    await worldview.ingest_node(state)
    g = await graphiti_service.get_graphiti()
    await g.close()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
