"""Render each LangGraph pipeline to a PNG.

Importable: `render_graphs()` returns `{filename: png_bytes}` for every
pipeline, so callers (e.g. the admin dashboard) can render on demand without
touching disk. Run as a script to additionally write the PNGs to the repo root.
"""

from app.services import llm, worldview, userfacts

GRAPHS = {
    "graph_llm.png": ("Responder / summarizer", lambda: llm._graph),
    "graph_worldview.png": ("World view", lambda: worldview._graph),
    "graph_userfacts.png": ("User facts / profile", lambda: userfacts._graph),
}


def render_graphs() -> dict[str, bytes]:
    """Render every pipeline graph to PNG bytes, keyed by filename."""
    return {
        filename: get_graph().get_graph().draw_mermaid_png()
        for filename, (_label, get_graph) in GRAPHS.items()
    }


if __name__ == "__main__":
    for filename, png in render_graphs().items():
        with open(filename, "wb") as f:
            f.write(png)
        print(f"wrote {filename}")
