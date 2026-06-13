"""Rachel's persistent "world view" — a flat list of facts she has learned.

This is a self-contained LangGraph pipeline that runs once per conversation,
after the CHAT_BLACKOUT_TIME flush (see app/telegram/client.py::finalize_conversation).

Two nodes run **sequentially**:

  START → fact_extractor_node → (any new facts?) → consolidation_node → END
                                       └── no ──→ END

- fact_extractor_node: reads the just-finished dialogue and pulls out new,
  permanent facts about the world. Returns nothing if it learned
  nothing new (short-circuits straight to END — no file write).
- consolidation_node: reads the *existing* facts plus the freshly extracted
  ones, then de-duplicates and resolves conflicts. Newer information always
  wins. Returns the full, rewritten fact set.

Storage is a plain markdown file (one fact per `- ` bullet line). There is a
single global file for now; per-user files are a future expansion. All facts
are assumed to fit comfortably in one context window, so there is no search /
retrieval step — the whole file is read and rewritten each time.

Readers of the file:  responder_node (app/services/llm.py), consolidation_node.
Writers of the file:  consolidation_node (via update_worldview).
"""

import asyncio
import traceback
from pathlib import Path
from typing import Any, Dict, List

import tiktoken

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from telethon.custom import conversation
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import CONSOLIDATION_SYSTEM_PROMPT, FACT_EXTRACTOR_SYSTEM_PROMPT

settings = get_settings()
BOT_NAME = settings.bot_name
_WORLDVIEW_PATH = Path(settings.worldview_path)

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))

# The file is read-modify-written as a whole, so serialise concurrent
# consolidations (two chats finishing near-simultaneously) to avoid clobbering.
_file_lock = asyncio.Lock()


# --- Structured outputs ------------------------------------------------------


class ExtractorOutput(BaseModel):
    facts: List[str] = Field(
        default_factory=list,
        description="New, permanent facts as short standalone sentences. Empty if nothing new was learned.",
    )


class ConsolidationOutput(BaseModel):
    facts: List[str] = Field(
        default_factory=list,
        description="The full, de-duplicated and conflict-resolved fact set as short sentences.",
    )


class WorldviewState(TypedDict):
    conversation: List[Dict[str, Any]]
    existing_facts: List[str]
    extracted_facts: List[str]
    consolidated_facts: List[str]
    # Originating chat, threaded through purely so node logs can be tagged and
    # disambiguated when multiple chats finalize concurrently.
    chat_id: int | None


def _tag(chat_id: int | None) -> str:
    """Log prefix for worldview lines, with the chat id when we know it."""
    return f"[worldview][{chat_id}]" if chat_id is not None else "[worldview]"


_extractor_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.0,
).with_structured_output(ExtractorOutput)

_consolidation_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.0,
).with_structured_output(ConsolidationOutput)


# --- File helpers ------------------------------------------------------------


def read_worldview() -> str:
    """Return the raw markdown body of the world view (for prompt injection).

    Empty string when the file does not exist yet.
    """
    if not _WORLDVIEW_PATH.exists():
        return ""
    return _WORLDVIEW_PATH.read_text(encoding="utf-8").strip()


def _read_facts() -> List[str]:
    """Parse the markdown file into a list of fact strings (one per bullet)."""
    facts: List[str] = []
    for line in read_worldview().splitlines():
        line = line.strip()
        if line.startswith("- "):
            facts.append(line[2:].strip())
        elif line and not line.startswith("#"):
            facts.append(line)
    return [f for f in facts if f]


def _write_facts(facts: List[str]) -> None:
    """Overwrite the markdown file with the given facts (one per bullet)."""
    body = "# Rachel's world view\n\n" + "\n".join(f"- {f}" for f in facts) + "\n"
    _WORLDVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WORLDVIEW_PATH.write_text(body, encoding="utf-8")


# --- Nodes -------------------------------------------------------------------


async def fact_extractor_node(state: WorldviewState) -> Dict:
    tag = _tag(state.get("chat_id"))
    # History is built as concrete Message objects (not templated tuples) so any
    # literal '{' or '}' in user content is passed through verbatim rather than
    # parsed as an f-string placeholder, which would crash from_messages.
    history_msgs = [
        AIMessage(content=f"{entry['sender']}: {entry['content']}")
        if entry["sender"] == BOT_NAME
        else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
        for entry in state["conversation"]
    ]
    system_msgs = ChatPromptTemplate.from_messages(
        [("system", FACT_EXTRACTOR_SYSTEM_PROMPT)]
    ).format_messages(bot_name=BOT_NAME)
    msgs = [*system_msgs, *history_msgs]
    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"{tag} extractor context: {len(msgs)} messages, {msgs_tokens} tokens")
    result: ExtractorOutput = await _extractor_llm.ainvoke(msgs)
    facts = [f.strip() for f in result.facts if f.strip()]
    print(f"{tag} extracted {len(facts)} new fact(s): {facts}")
    return {"extracted_facts": facts}


async def consolidation_node(state: WorldviewState) -> Dict:
    tag = _tag(state.get("chat_id"))
    existing = state["existing_facts"]
    extracted = state["extracted_facts"]

    existing_facts_text = "\n".join(f"- {f}" for f in existing) or "(none)"
    new_facts_text = "\n".join(f"- {f}" for f in extracted)

    prompt = ChatPromptTemplate.from_messages([("system", CONSOLIDATION_SYSTEM_PROMPT)])
    msgs = prompt.format_messages(
        bot_name=BOT_NAME, existing_facts=existing_facts_text, new_facts=new_facts_text
    )
    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"{tag} consolidation context: {len(msgs)} messages, {msgs_tokens} tokens")
    result: ConsolidationOutput = await _consolidation_llm.ainvoke(msgs)
    facts = [f.strip() for f in result.facts if f.strip()]
    print(f"{tag} consolidated to {len(facts)} fact(s)")
    return {"consolidated_facts": facts}


def _route_after_extraction(state: WorldviewState) -> str:
    """Skip consolidation (and the file write) when nothing new was learned."""
    return "consolidation_node" if state["extracted_facts"] else END


def _build_graph():
    graph: StateGraph = StateGraph(WorldviewState)
    graph.add_node("fact_extractor_node", fact_extractor_node)
    graph.add_node("consolidation_node", consolidation_node)
    graph.add_edge(START, "fact_extractor_node")
    graph.add_conditional_edges(
        "fact_extractor_node",
        _route_after_extraction,
        {"consolidation_node": "consolidation_node", END: END},
    )
    graph.add_edge("consolidation_node", END)
    return graph.compile()


_graph = _build_graph()


async def update_worldview(conversation: List[Dict[str, str]], chat_id: int | None = None) -> List[str] | None:
    """Extract facts from a finished conversation and merge them into the file.

    Returns the new full fact set if the file was rewritten, else None (nothing
    new was learned). The whole read-modify-write is held under _file_lock so
    concurrent conversations cannot clobber each other.
    """
    if not conversation:
        return None

    tag = _tag(chat_id)
    async with _file_lock:
        existing_facts = _read_facts()
        state: WorldviewState = {
            "conversation": conversation,
            "existing_facts": existing_facts,
            "extracted_facts": [],
            "consolidated_facts": [],
            "chat_id": chat_id,
        }
        try:
            result = await _graph.ainvoke(state)
        except Exception as e:  # never let memory upkeep crash the caller
            print(f"{tag} update failed: {e}")
            traceback.print_exc()
            return None


        if not result["extracted_facts"]:
            print(f"{tag} nothing new from chat; file unchanged")
            return None

        new_facts = result["consolidated_facts"]
        _write_facts(new_facts)
        print(f"{tag} file updated ({len(new_facts)} fact(s)) after chat")
        return new_facts
