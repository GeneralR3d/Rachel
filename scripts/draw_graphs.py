"""Render each LangGraph pipeline to a PNG in the repo root."""

from app.services import llm, worldview, userfacts

GRAPHS = {
    "graph_llm.png": llm._graph,
    "graph_worldview.png": worldview._graph,
    "graph_userfacts.png": userfacts._graph,
}

for filename, graph in GRAPHS.items():
    png = graph.get_graph().draw_mermaid_png()
    with open(filename, "wb") as f:
        f.write(png)
    print(f"wrote {filename}")
