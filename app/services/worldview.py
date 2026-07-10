"""Rachel's persistent "world view" — durable, general facts she has learned.

This is a self-contained LangGraph pipeline that runs once per conversation,
after the CHAT_BLACKOUT_TIME flush (see app/telegram/client.py::finalize_conversation).

Two nodes run **sequentially**:

  START → fact_extractor_node → (any new facts?) → ingest_node → END
                                       └── no ──→ END

- fact_extractor_node: reads the just-finished dialogue and pulls out new,
  permanent facts about the world. Returns nothing if it learned nothing new
  (short-circuits straight to END — no graph write).
- ingest_node: writes each extracted fact into **Graphiti** (a temporal
  knowledge graph backed by Neo4j) as one episode per fact. Graphiti performs
  its own entity/edge extraction, de-duplication, and temporal conflict
  resolution on ingest, so there is no hand-rolled consolidation step anymore.

Retrieval: ``search_worldview()`` runs a Graphiti hybrid search and returns the
matching facts as a ``- ``-bulleted string; it is exposed to the context_fetcher
node as the ``search_world_view`` LangChain tool. The context_fetcher generates a
conversation-aware query and the result is threaded into the responder as
``{world_view}`` (see app/services/llm.py).
"""

import traceback
from pathlib import Path
from typing import Any, Dict, List

import tiktoken

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import FACT_EXTRACTOR_SYSTEM_PROMPT
from app.services.graphiti import ingest_facts, list_episodes, search_graph
from app.services.metrics import LLM_CALLS, record_llm_error

settings = get_settings()
BOT_NAME = settings.bot_name
_WORLDVIEW_PATH = Path(settings.worldview_path)

# Graphiti partition for Rachel's general world-view memory. All world-view
# episodes are written under this group_id and searches are scoped to it, so
# that the per-user memory partitions (see userfacts.user_facts_group_id) never
# clash with it or each other.
_WORLDVIEW_GROUP_ID = "worldview"

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))


# --- Structured outputs ------------------------------------------------------


class ExtractorOutput(BaseModel):
    facts: List[str] = Field(
        default_factory=list,
        description="New, permanent facts as short standalone sentences. Empty if nothing new was learned.",
    )


class WorldviewState(TypedDict):
    # Pre-divided extraction history built once upstream (in
    # app/services/memory.py::update_memories): the conversation rendered as LLM
    # turns with the "already-processed" divider already inserted. The fact
    # extractor reads these directly instead of re-dividing.
    extraction_msgs: List[Any]
    # False when every message is at or below the watermark (nothing new to
    # extract), letting fact_extractor_node short-circuit.
    has_new: bool
    extracted_facts: List[str]
    # Originating chat, threaded through purely so node logs can be tagged and
    # disambiguated when multiple chats finalize concurrently.
    chat_id: int | None


def _tag(chat_id: int | None) -> str:
    """Log prefix for worldview lines, with the chat id when we know it."""
    return f"[worldview][{chat_id}]" if chat_id is not None else "[worldview]"


# Uses the Graphiti key too: this extractor is the front half of the same
# world-view memory pipeline that feeds Graphiti, so all its cost is billed to
# the same separate OpenRouter key.
_extractor_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.graphiti_api_key,
    temperature=0.0,
).with_structured_output(ExtractorOutput)


# --- Retrieval ---------------------------------------------------------------


async def search_worldview(query: str | None = None) -> str:
    """Search the world-view partition; see ``graphiti.search_graph`` for semantics."""
    return await search_graph(query, _WORLDVIEW_GROUP_ID, "[worldview]")


@tool
async def search_world_view(query: str) -> str:
    """Search Rachel's world-view knowledge base of general facts.

    The world-view database stores short, single-sentence general facts Rachel has
    learned about the world from past conversations (about brands, places, people,
    events. Use this to look up what Rachel already knows about whatever the conversation is touching on, so the
    responder can reply accurately instead of guessing.

    Args:
        query: A focused, natural-language description of what to look up, derived
            from what the conversation is about (e.g. "Chagee bubble tea brand").
            Do not just copy the last message verbatim — capture the topic/entity.
    """
    return await search_worldview(query) or "No relevant facts found."


# --- Storage access (Graphiti) ------------------------------------------------


async def add_worldview_facts(facts: List[str]) -> None:
    """
    Setter method.
    Ingest new general facts into the world-view Graphiti partition.

    One episode per fact; Graphiti's own dedup / temporal conflict resolution
    merges them against what is already stored. Used by the pipeline's
    ingest_node path and by the admin surfaces (HTTP API). Slow — each fact is
    several LLM round-trips (see graphiti.ingest_facts).
    """
    await ingest_facts(
        facts,
        group_id=_WORLDVIEW_GROUP_ID,
        source_description="Rachel worldview fact",
        name_prefix="worldview",
    )


async def get_worldview_facts() -> List[str]:
    """
    Getter method.
    Return every fact episode stored in the world view, oldest first.

    Reads the raw ingested sentences straight off the worldview partition — the
    full dump, not a search. Used by the admin surfaces (HTTP API).
    """
    return await list_episodes(_WORLDVIEW_GROUP_ID)


# --- Nodes -------------------------------------------------------------------


async def fact_extractor_node(state: WorldviewState) -> Dict:
    tag = _tag(state.get("chat_id"))
    # extraction_msgs is pre-divided upstream: older (already-processed) messages
    # sit above the divider as context, only newer ones below are to be extracted.
    if not state.get("has_new"):
        print(f"{tag} no new messages since last processed; skipping extraction")
        return {"extracted_facts": []}
    history_msgs = state["extraction_msgs"]
    system_msgs = ChatPromptTemplate.from_messages(
        [("system", FACT_EXTRACTOR_SYSTEM_PROMPT)]
    ).format_messages(bot_name=BOT_NAME)
    msgs = [*system_msgs, *history_msgs]
    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"{tag} extractor context: {len(msgs)} messages, {msgs_tokens} tokens")
    LLM_CALLS.labels(node="worldview_extractor").inc()
    try:
        result: ExtractorOutput = await _extractor_llm.ainvoke(msgs)
    except Exception as e:
        # Record then re-raise: the graph-level handler in update_worldview logs
        # and fails open (returns None), so behaviour here is unchanged — we only
        # want the failure counted/classified before it propagates.
        kind = record_llm_error("worldview_extractor", e)
        print(f"{tag} extractor LLM call failed ({kind}: {type(e).__name__}: {e})")
        raise
    facts = [f.strip() for f in result.facts if f.strip()]
    print(f"{tag} extracted {len(facts)} new fact(s): {facts}")
    return {"extracted_facts": facts}


async def ingest_node(state: WorldviewState) -> Dict:
    """Write each extracted fact into Graphiti as its own episode (see ingest_facts)."""
    tag = _tag(state.get("chat_id"))
    facts = state["extracted_facts"]
    # World-view facts are general knowledge, not chat-scoped, so the episode
    # name is deliberately chat-agnostic — only timestamped to stay unique.
    await add_worldview_facts(facts)
    print(f"{tag} ingested {len(facts)} fact(s) into Graphiti")
    return {}


def _route_after_extraction(state: WorldviewState) -> str:
    """Skip ingestion when nothing new was learned."""
    return "ingest_node" if state["extracted_facts"] else END


def _build_graph():
    graph: StateGraph = StateGraph(WorldviewState)
    graph.add_node("fact_extractor_node", fact_extractor_node)
    graph.add_node("ingest_node", ingest_node)
    graph.add_edge(START, "fact_extractor_node")
    graph.add_conditional_edges(
        "fact_extractor_node",
        _route_after_extraction,
        {"ingest_node": "ingest_node", END: END},
    )
    graph.add_edge("ingest_node", END)
    return graph.compile()


_graph = _build_graph()


async def update_worldview(
    extraction_msgs: List[Any],
    has_new: bool,
    chat_id: int | None = None,
) -> List[str] | None:
    """Extract facts from a finished conversation and ingest them into Graphiti.

    ``extraction_msgs`` is the pre-divided extraction history and ``has_new``
    whether it contains any not-yet-processed messages — both built once upstream
    by app/services/memory.py::update_memories (the divider helper is no longer
    called here).

    Returns the list of newly ingested facts, or None when nothing new was
    learned (or on error). Never raises — memory upkeep must not crash the caller.
    """
    if not extraction_msgs:
        return None

    tag = _tag(chat_id)
    state: WorldviewState = {
        "extraction_msgs": extraction_msgs,
        "has_new": has_new,
        "extracted_facts": [],
        "chat_id": chat_id,
    }
    try:
        result = await _graph.ainvoke(state)
    except Exception as e:  # never let memory upkeep crash the caller
        print(f"{tag} update failed: {e}")
        traceback.print_exc()
        return None

    facts = result["extracted_facts"]
    if not facts:
        print(f"{tag} nothing new from chat; graph unchanged")
        return None

    print(f"{tag} graph updated ({len(facts)} fact(s)) after chat")
    return facts
