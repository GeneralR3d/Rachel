"""Per-user facts & preferences — Rachel's personal memory of each individual.

This is a self-contained LangGraph pipeline that runs once per conversation,
after the CHAT_BLACKOUT_TIME flush (see app/telegram/client.py::finalize_conversation).
It is the user-specific counterpart to app/services/worldview.py: where the
world-view pipeline keeps only *general*, non-personal facts, this one keeps the
*personal* facts and preferences about each individual user.

Flow:

  START → fact_extractor_node → (any new facts?) → consolidation_node (×N) → END
                                       └── no ──→ END

- fact_extractor_node: reads the just-finished dialogue once and pulls out new,
  durable personal facts/preferences, **grouped by the user they are about**.
  Returns nothing if it learned nothing new (short-circuits straight to END).
- consolidation_node: fanned out **in parallel, one instance per user** that had
  new facts. Each instance reads that user's existing facts, merges in the newly
  extracted ones (de-dup + conflict resolution, newer wins), and writes the
  rewritten profile back.

Storage is the per-user ``user_facts_preferences`` table, read/written via
``get_user_facts`` / ``set_user_facts`` in app/repository.py — there is no
markdown file. Each profile is assumed to fit comfortably in one context window,
so the whole profile is read and rewritten each time.
"""

import asyncio
import traceback
from collections import defaultdict
from typing import Any, Dict, List
from datetime import datetime
from pprint import pprint

import tiktoken

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import (
    USER_FACT_CONSOLIDATION_SYSTEM_PROMPT,
    USER_FACT_EXTRACTOR_SYSTEM_PROMPT,
)
from app.repository import get_user_facts, set_user_facts

settings = get_settings()
BOT_NAME = settings.bot_name

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))

# Per-user lock guarding the read-modify-write in consolidation_node. Two
# conversations finishing at once that both involve the same user would
# otherwise race (both read the old profile, second write clobbers the first).
# Single-process/single-event-loop only — switch to a PG advisory lock if this
# ever runs across multiple workers.
_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# --- Structured outputs ------------------------------------------------------


class UserFacts(BaseModel):
    sender: str = Field(
        description="The sender name (exactly as shown in the conversation) the facts are about."
    )
    facts: List[str] = Field(
        default_factory=list,
        description="New personal facts/preferences about this user as short standalone sentences.",
    )


class ExtractorOutput(BaseModel):
    user_facts: List[UserFacts] = Field(
        default_factory=list,
        description="One entry per user who yielded new facts. Empty if nothing new was learned.",
    )


class ConsolidationOutput(BaseModel):
    facts: List[str] = Field(
        default_factory=list,
        description="The full, de-duplicated and conflict-resolved profile as short sentences.",
    )


class UserFactsState(TypedDict):
    conversation: List[Dict[str, Any]]
    # Maps each sender name in the conversation to its numeric user_id.
    name_to_id: Dict[str, int]
    # Narrative summary of the just-finished conversation, injected into the
    # extractor prompt as {chat_summary} to enrich extractions.
    summary: str
    # Extracted new facts keyed by user_id.
    extracted: Dict[int, List[str]]


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


# --- Fact text <-> list helpers ----------------------------------------------


def _parse_facts(text: str) -> List[str]:
    """Parse a stored profile (one fact per `- ` bullet line) into a list."""
    facts: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            facts.append(line[2:].strip())
        elif line and not line.startswith("#"):
            facts.append(line)
    return [f for f in facts if f]


def _format_facts(facts: List[str]) -> str:
    """Render a list of facts as bullet lines for storage."""
    return "\n".join(f"- {f}" for f in facts)


# --- Nodes -------------------------------------------------------------------


async def fact_extractor_node(state: UserFactsState) -> Dict:
    # Only the sender NAME is shown to the model — it is far less likely to
    # hallucinate a name than a numeric id. We resolve names back to ids below
    # via name_to_id.
    # History is built as concrete Message objects (not templated tuples) so any
    # literal '{' or '}' in user content is passed through verbatim rather than
    # parsed as an f-string placeholder, which would crash from_messages.
    history_msgs = [
        AIMessage(content=f"{entry['sender']}: {entry['content']}")
        if entry["sender"] == BOT_NAME
        else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
        for entry in state["conversation"]
    ]

    now = datetime.now()
    formatted_date = (
        f"The current date is {now.strftime('%d %B %Y')}, "
        f"the current month is {now.strftime('%B')}, "
        f"the current day of week is {now.strftime('%A')}. "
    )

    system_msgs = ChatPromptTemplate.from_messages(
        [("system", USER_FACT_EXTRACTOR_SYSTEM_PROMPT)]
    ).format_messages(
        bot_name=BOT_NAME, chat_summary=state.get("summary") or "(none)", observation_date=formatted_date
    )
    msgs = [*system_msgs, *history_msgs]
    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"[userfacts] extractor context: {len(msgs)} messages, {msgs_tokens} tokens")
    result: ExtractorOutput = await _extractor_llm.ainvoke(msgs)

    name_to_id = state["name_to_id"]
    extracted: Dict[int, List[str]] = {}
    for entry in result.user_facts:
        facts = [f.strip() for f in entry.facts if f.strip()]
        if not facts:
            continue
        user_id = name_to_id.get(entry.sender)
        if user_id is None:
            # Model returned a name we don't recognise (e.g. Rachel herself or a
            # hallucinated name) — drop it rather than guess an id.
            print(f"[userfacts] skipping unknown sender {entry.sender!r}")
            continue
        extracted.setdefault(user_id, []).extend(facts)

    for uid, facts in extracted.items():
        print(f"[userfacts] extracted facts for user {uid}")
        pprint(facts)
    return {"extracted": extracted}


async def consolidation_node(payload: Dict) -> Dict:
    """Merge one user's new facts into their stored profile and persist it.

    Fanned out one instance per user via Send, so each call handles a single
    user_id and writes independently.
    """
    user_id: int = payload["user_id"]
    extracted: List[str] = payload["new_facts"]

    # Serialise the whole read-modify-write per user so concurrent conversations
    # touching the same user can't clobber each other's profile.
    async with _user_locks[user_id]:
        existing = _parse_facts(await get_user_facts(user_id))

        existing_facts_text = "\n".join(f"- {f}" for f in existing) or "(none)"
        new_facts_text = "\n".join(f"- {f}" for f in extracted)

        prompt = ChatPromptTemplate.from_messages(
            [("system", USER_FACT_CONSOLIDATION_SYSTEM_PROMPT)]
        )
        msgs = prompt.format_messages(
            bot_name=BOT_NAME, existing_facts=existing_facts_text, new_facts=new_facts_text
        )
        msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
        print(f"[userfacts] consolidation context (user {user_id}): {len(msgs)} messages, {msgs_tokens} tokens")
        result: ConsolidationOutput = await _consolidation_llm.ainvoke(msgs)
        facts = [f.strip() for f in result.facts if f.strip()]

        await set_user_facts(user_id, _format_facts(facts))
        print(f"[userfacts] user {user_id} profile updated ({len(facts)} fact(s))")
    return {}


def _route_after_extraction(state: UserFactsState):
    """Fan out one consolidation_node per user; skip entirely if nothing new."""
    extracted = state["extracted"]
    if not extracted:
        return END
    return [
        Send("consolidation_node", {"user_id": user_id, "new_facts": facts})
        for user_id, facts in extracted.items()
    ]


def _build_graph():
    graph: StateGraph = StateGraph(UserFactsState)
    graph.add_node("fact_extractor_node", fact_extractor_node)
    graph.add_node("consolidation_node", consolidation_node)
    graph.add_edge(START, "fact_extractor_node")
    graph.add_conditional_edges(
        "fact_extractor_node",
        _route_after_extraction,
        ["consolidation_node", END],
    )
    graph.add_edge("consolidation_node", END)
    return graph.compile()


_graph = _build_graph()


async def update_user_facts(
    conversation: List[Dict[str, Any]], summary: str = ""
) -> None:
    """Extract per-user facts from a finished conversation and merge each into DB.

    Never raises — errors are caught and logged so memory upkeep can't crash the
    caller. Per-user consolidations run in parallel and each writes its own row,
    so there is no shared file to serialise.
    """
    if not conversation:
        return

    # Build a sender-name -> user_id map so the model only ever has to produce
    # the (low-hallucination) name; we resolve the id ourselves. The bot's own
    # turns are excluded — we never store facts about Rachel.
    name_to_id = {
        entry["sender"]: entry["sender_user_id"]
        for entry in conversation
        if entry["sender"] != BOT_NAME
    }

    state: UserFactsState = {
        "conversation": conversation,
        "name_to_id": name_to_id,
        "summary": summary or "",
        "extracted": {},
    }
    try:
        await _graph.ainvoke(state)
    except Exception as e:  # never let memory upkeep crash the caller
        print(f"[userfacts] update failed: {e}")
        traceback.print_exc()
