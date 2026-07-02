"""One-time helper: wipe the ENTIRE Graphiti / Neo4j graph.

This deletes every node and relationship in the database (all group_ids —
world-view AND any future per-user partitions), via Graphiti's supported
``clear_data`` (``MATCH (n) DETACH DELETE n``). Indexes/constraints are left
intact; the app rebuilds them idempotently on next startup anyway.

IRREVERSIBLE. There is no undo. Run from the repo root (Neo4j must be up):

    uv run python -m scripts.clear_graph

You will be asked to type the exact confirmation phrase before anything is
deleted. Pass --yes to skip the prompt (e.g. for non-interactive use):

    uv run python -m scripts.clear_graph --yes
"""

import asyncio
import sys
from pathlib import Path

# Allow running as a plain file, not just as a module: put the repo root on
# sys.path so `app` imports resolve regardless of invocation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphiti_core.utils.maintenance.graph_data_operations import clear_data

from app.config import get_settings
from app.services import worldview

_CONFIRM_PHRASE = "delete everything"


async def wipe() -> None:
    settings = get_settings()
    graphiti = await worldview._get_graphiti()
    print(
        f"Wiping ALL data from Neo4j at {settings.neo4j_uri} "
        f"(database 'neo4j') — every group_id, not just 'worldview'."
    )
    # group_ids=None => MATCH (n) DETACH DELETE n over the whole graph.
    await clear_data(graphiti.driver, group_ids=None)
    print("Graph cleared. All nodes and relationships deleted.")


def main() -> None:
    skip_confirm = "--yes" in sys.argv[1:]
    if not skip_confirm:
        print(
            "This will PERMANENTLY delete the entire Graphiti graph (all nodes, "
            "edges, episodes — every group_id). This cannot be undone."
        )
        try:
            answer = input(f'Type "{_CONFIRM_PHRASE}" to proceed: ').strip()
        except EOFError:
            print("Aborted (no interactive input; re-run with --yes to force).")
            return
        if answer != _CONFIRM_PHRASE:
            print("Aborted — confirmation phrase did not match.")
            return

    asyncio.run(wipe())


if __name__ == "__main__":
    main()
