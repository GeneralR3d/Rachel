"""Per-user facts & preferences — Rachel's personal memory of each individual.

This is a self-contained LangGraph pipeline that runs once per conversation,
after the CHAT_BLACKOUT_TIME flush (see app/telegram/client.py::finalize_conversation).
It is the user-specific counterpart to app/services/worldview.py: where the
world-view pipeline keeps only *general*, non-personal facts, this one keeps the
*personal* facts and preferences about each individual user.

Flow:

  START → fact_extractor_node → (any new facts?) → consolidation_node (×N) → END
        →                              └── no ──→ END
        → profile_extraction_update_node ──────────────────────────────────→ END

- fact_extractor_node: reads the just-finished dialogue once and pulls out new,
  durable personal facts/preferences, **grouped by the user they are about**.
  Returns nothing if it learned nothing new (short-circuits straight to END).
- consolidation_node: fanned out **in parallel, one instance per user** that had
  new facts. Each instance reads that user's existing facts, merges in the newly
  extracted ones (de-dup + conflict resolution, newer wins), and writes the
  rewritten profile back.
- profile_extraction_update_node: the fixed-slot-profile counterpart, running on
  a separate START branch in parallel. It extracts the structured profile slots
  for every participant and, in the same node, writes each user's slots back —
  the merge is a deterministic field-level overwrite (newer non-empty value
  wins), so it needs no separate LLM consolidation pass or per-user fan-out.

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
from pydantic import BaseModel, Field, create_model
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import (
    USER_FACT_CONSOLIDATION_SYSTEM_PROMPT,
    USER_FACT_EXTRACTOR_SYSTEM_PROMPT,
    USER_PROFILE_EXTRACTOR_SYSTEM_PROMPT,
    USER_PROFILE_FIELDS,
)
from app.repository import (
    get_user_facts,
    get_user_profile,
    set_user_facts,
    set_user_profile,
)
from app.services.llm import (
    get_user_profiles_cached,
    update_user_facts_cache,
    update_user_profile_cache,
)

settings = get_settings()
BOT_NAME = settings.bot_name

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))


def _tag(chat_id: int | None) -> str:
    """Log prefix for userfacts lines, with the chat id when we know it."""
    return f"[userfacts][{chat_id}]" if chat_id is not None else "[userfacts]"


def _profile_tag(chat_id: int | None) -> str:
    """Log prefix for the user_profile node, with the chat id when we know it."""
    return f"[userprofile][{chat_id}]" if chat_id is not None else "[userprofile]"

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


# The structured-profile schema is built dynamically from USER_PROFILE_FIELDS so
# that file is the single source of truth: every slot becomes an optional ""
# string field whose description steers the extractor. Empty string = "no
# evidence for this slot", which the code-merge below treats as "leave as-is".
_PROFILE_KEYS = [key for key, _label, _guide in USER_PROFILE_FIELDS]
UserProfileFields = create_model(  # type: ignore[call-overload]
    "UserProfileFields",
    **{
        key: (str, Field(default="", description=guide))
        for key, _label, guide in USER_PROFILE_FIELDS
    },
)


class ProfileEntry(BaseModel):
    sender: str = Field(
        description="The sender name (exactly as shown in the conversation) the profile is about."
    )
    profile: UserProfileFields = Field(  # type: ignore[valid-type]
        description="Fixed profile slots. Fill only the ones with clear evidence; leave the rest as empty strings.",
    )


class ProfileExtractorOutput(BaseModel):
    profiles: List[ProfileEntry] = Field(
        default_factory=list,
        description="One entry per user with new/updated profile info. Empty if nothing profile-worthy was learned.",
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
    # Originating chat, threaded through purely so node logs can be tagged and
    # disambiguated when multiple chats finalize concurrently.
    chat_id: int | None


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

_profile_extractor_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.0,
).with_structured_output(ProfileExtractorOutput)

# Pre-rendered slot reference injected into the extractor prompt as
# {slot_descriptions} (one "- key (Label): guidance" line per field).
_SLOT_DESCRIPTIONS = "\n".join(
    f"- {key} ({label}): {guide}" for key, label, guide in USER_PROFILE_FIELDS
)


def _render_existing_profiles(profiles_by_name: Dict[str, Dict[str, str]]) -> str:
    """Render each participant's stored profile for the extractor prompt.

    Keyed by sender NAME (the only identifier the model ever sees). For each
    person we list their already-filled slots and call out the empty ones, so
    the model can skip known slots and focus on filling gaps / genuine updates.
    """
    if not profiles_by_name:
        return "(no participants)"
    blocks: List[str] = []
    for name, profile in profiles_by_name.items():
        lines = [f"{name}:"]
        filled = {
            key: str(profile.get(key, "")).strip()
            for key in _PROFILE_KEYS
            if str(profile.get(key, "")).strip()
        }
        if filled:
            lines.extend(f"  - {key}: {filled[key]}" for key in _PROFILE_KEYS if key in filled)
        else:
            lines.append("  (no profile information yet)")
        empty = [key for key in _PROFILE_KEYS if key not in filled]
        if empty:
            lines.append(f"  empty slots still needing info: {', '.join(empty)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
    tag = _tag(state.get("chat_id"))
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
    print(f"{tag} extractor context: {len(msgs)} messages, {msgs_tokens} tokens")
    result: ExtractorOutput = await _extractor_llm.ainvoke(msgs)

    name_to_id = state["name_to_id"]
    # Log exactly what the model returned vs. what names we can resolve, so the
    # three "nothing happened" cases (model returned []; names didn't resolve;
    # genuine facts) are distinguishable in the logs.
    returned = [(e.sender, len([f for f in e.facts if f.strip()])) for e in result.user_facts]
    print(
        f"{tag} extractor returned {len(result.user_facts)} entry(ies): {returned} | "
        f"resolvable names: {list(name_to_id.keys())}"
    )

    extracted: Dict[int, List[str]] = {}
    for entry in result.user_facts:
        facts = [f.strip() for f in entry.facts if f.strip()]
        if not facts:
            continue
        user_id = name_to_id.get(entry.sender)
        if user_id is None:
            # Model returned a name we don't recognise (e.g. Rachel herself or a
            # hallucinated name) — drop it rather than guess an id.
            print(
                f"{tag} skipping unknown sender {entry.sender!r} "
                f"({len(facts)} fact(s) dropped); known names: {list(name_to_id.keys())}"
            )
            continue
        extracted.setdefault(user_id, []).extend(facts)

    if not extracted:
        print(f"{tag} nothing to consolidate (no resolvable new facts), ending")
    for uid, facts in extracted.items():
        print(f"{tag} extracted facts for user {uid}")
        pprint(facts)
    return {"extracted": extracted}


async def consolidation_node(payload: Dict) -> Dict:
    """Merge one user's new facts into their stored profile and persist it.

    Fanned out one instance per user via Send, so each call handles a single
    user_id and writes independently.
    """
    user_id: int = payload["user_id"]
    extracted: List[str] = payload["new_facts"]
    tag = _tag(payload.get("chat_id"))

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
        print(f"{tag} consolidation context (user {user_id}): {len(msgs)} messages, {msgs_tokens} tokens")
        result: ConsolidationOutput = await _consolidation_llm.ainvoke(msgs)
        facts = [f.strip() for f in result.facts if f.strip()]

        facts_text = _format_facts(facts)
        await set_user_facts(user_id, facts_text)
        # Write-through the responder's cache so it doesn't serve the stale
        # pre-consolidation profile for up to USER_FACTS_CACHE_TTL.
        update_user_facts_cache(user_id, facts_text)
        print(f"{tag} user {user_id} profile updated ({len(facts)} fact(s))")
    return {}


async def profile_extraction_update_node(state: UserFactsState) -> Dict:
    """Extract fixed structured-profile slots per participant and write them back.

    Runs in parallel with fact_extractor_node (separate START branch): the
    free-form facts and the fixed-slot profile are independent kinds of memory.

    Unlike the free-form facts branch, this node does both extraction *and* the
    write in one place — the profile merge is a deterministic field-level
    overwrite (newer non-empty value wins), so no separate LLM consolidation pass
    or per-user fan-out is needed. For each participant with new slots we
    read-modify-write their profile under the per-user lock and write through the
    responder's cache.
    """
    tag = _profile_tag(state.get("chat_id"))
    name_to_id = state["name_to_id"]
    history_msgs = [
        AIMessage(content=f"{entry['sender']}: {entry['content']}")
        if entry["sender"] == BOT_NAME
        else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
        for entry in state["conversation"]
    ]

    # Show the model each participant's CURRENT stored profile so it can skip
    # already-known slots and concentrate on the empty ones / genuine updates.
    # This read is context only and served from the responder's profile cache
    # (the same one the responder reads) — staleness here only nudges what the
    # model sees, never what gets persisted: the authoritative read happens again
    # from the DB inside the per-user lock below, just before the write.
    names = list(name_to_id.keys())
    profiles_by_id = await get_user_profiles_cached([name_to_id[name] for name in names])
    existing_profiles_text = _render_existing_profiles(
        {name: profiles_by_id.get(name_to_id[name], {}) for name in names}
    )

    system_msgs = ChatPromptTemplate.from_messages(
        [("system", USER_PROFILE_EXTRACTOR_SYSTEM_PROMPT)]
    ).format_messages(
        bot_name=BOT_NAME,
        chat_summary=state.get("summary") or "(none)",
        slot_descriptions=_SLOT_DESCRIPTIONS,
        existing_profiles=existing_profiles_text,
    )
    msgs = [*system_msgs, *history_msgs]
    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"{tag} profile extractor context: {len(msgs)} messages, {msgs_tokens} tokens")
    result: ProfileExtractorOutput = await _profile_extractor_llm.ainvoke(msgs)

    returned = [
        (e.sender, len([k for k in _PROFILE_KEYS if getattr(e.profile, k, "").strip()]))
        for e in result.profiles
    ]
    print(
        f"{tag} profile extractor returned {len(result.profiles)} entry(ies): {returned} | "
        f"resolvable names: {list(name_to_id.keys())}"
    )

    profile_extracted: Dict[int, Dict[str, str]] = {}
    for entry in result.profiles:
        # Keep only slots the model actually filled (non-empty after strip).
        slots = {
            key: getattr(entry.profile, key).strip()
            for key in _PROFILE_KEYS
            if getattr(entry.profile, key, "").strip()
        }
        if not slots:
            continue
        user_id = name_to_id.get(entry.sender)
        if user_id is None:
            print(
                f"{tag} skipping unknown sender {entry.sender!r} "
                f"({len(slots)} profile slot(s) dropped); known names: {list(name_to_id.keys())}"
            )
            continue
        # Last write wins if the same user somehow appears twice.
        profile_extracted.setdefault(user_id, {}).update(slots)

    if not profile_extracted:
        print(f"{tag} no resolvable profile slots, ending profile branch")
        return {}

    for user_id, new_slots in profile_extracted.items():
        print(f"{tag} extracted profile slots for user {user_id}")
        pprint(new_slots)
        # Read-modify-write each user's profile under the per-user lock so the
        # two branches (and concurrent finalizations) can't interleave on the
        # same user's row.
        async with _user_locks[user_id]:
            existing = await get_user_profile(user_id)
            # Drop any keys no longer in the schema, then overlay the new slots.
            merged = {k: v for k, v in existing.items() if k in _PROFILE_KEYS}
            merged.update(new_slots)
            await set_user_profile(user_id, merged)
            update_user_profile_cache(user_id, merged)
            print(
                f"{tag} user {user_id} profile slots updated "
                f"({len(new_slots)} changed, {len(merged)} total)"
            )
    return {}


def _route_after_extraction(state: UserFactsState):
    """Fan out one consolidation_node per user; skip entirely if nothing new."""
    extracted = state["extracted"]
    if not extracted:
        return END
    chat_id = state.get("chat_id")
    return [
        Send("consolidation_node", {"user_id": user_id, "new_facts": facts, "chat_id": chat_id})
        for user_id, facts in extracted.items()
    ]


def _build_graph():
    graph: StateGraph = StateGraph(UserFactsState)
    graph.add_node("fact_extractor_node", fact_extractor_node)
    graph.add_node("consolidation_node", consolidation_node)
    graph.add_node("profile_extraction_update_node", profile_extraction_update_node)

    # Two independent branches fan out from START in parallel: free-form facts
    # and the fixed-slot profile. They read the same conversation but write to
    # different storage (facts text column vs. profile JSONB), then join at END.
    #
    # The facts branch still splits extraction from a per-user LLM consolidation
    # fan-out; the profile branch does extraction + a deterministic field-level
    # write in a single node, so it needs no routing or fan-out of its own.
    graph.add_edge(START, "fact_extractor_node")
    graph.add_conditional_edges(
        "fact_extractor_node",
        _route_after_extraction,
        ["consolidation_node", END],
    )
    graph.add_edge("consolidation_node", END)

    graph.add_edge(START, "profile_extraction_update_node")
    graph.add_edge("profile_extraction_update_node", END)
    return graph.compile()


_graph = _build_graph()


async def update_user_facts(
    conversation: List[Dict[str, Any]], summary: str = "", chat_id: int | None = None
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
        "chat_id": chat_id,
    }
    try:
        await _graph.ainvoke(state)
    except Exception as e:  # never let memory upkeep crash the caller
        print(f"{_tag(chat_id)} update failed: {e}")
        traceback.print_exc()
