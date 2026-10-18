"""Render each workflow diagram to a PNG.

Importable: `render_graphs()` returns `{filename: png_bytes}` for every
pipeline, so callers (e.g. the admin dashboard) can render on demand without
touching disk. Run as a script to additionally write the PNGs to the repo root.
The reply diagram uses a hand-arranged Mermaid source to keep its gate,
parallel branches, and retrieval loop readable.
"""

from pathlib import Path

from langchain_core.runnables.graph_mermaid import draw_mermaid_png

from app.services import llm, worldview, userfacts

LLM_MERMAID = Path(__file__).with_name("graph_llm.mmd")

GRAPHS = {
    "graph_llm.png": ("Responder / summarizer", lambda: llm._graph),
    "graph_proactive.png": ("Proactive outreach", lambda: llm._proactive_graph),
    "graph_worldview.png": ("World view", lambda: worldview._graph),
    "graph_userfacts.png": ("User facts / profile", lambda: userfacts._graph),
}


def render_graphs() -> dict[str, bytes]:
    """Render every pipeline graph to PNG bytes, keyed by filename."""
    rendered = {
        "graph_llm.png": draw_mermaid_png(LLM_MERMAID.read_text())
    }
    rendered.update({
        filename: get_graph().get_graph().draw_mermaid_png()
        for filename, (_label, get_graph) in GRAPHS.items()
        if filename != "graph_llm.png"
    })
    return rendered


if __name__ == "__main__":
    for filename, png in render_graphs().items():
        with open(filename, "wb") as f:
            f.write(png)
        print(f"wrote {filename}")
