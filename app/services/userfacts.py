"""Per-user facts & preferences — Rachel's personal memory of each individual.

This is a self-contained LangGraph pipeline that runs once per conversation,
after the CHAT_BLACKOUT_TIME flush (see app/telegram/client.py::finalize_conversation).
It is the user-specific counterpart to app/services/worldview.py: where the
world-view pipeline keeps only *general*, non-personal facts, this one keeps the
*personal* facts and preferences about each individual user.

Flow:

  START → fact_extractor_node → (any new facts?) → ingest_node → END
        →                              └── no ──→ END
        → profile_extraction_update_node ────────────────────→ END

- fact_extractor_node: reads the just-finished dialogue once and pulls out new,
  durable personal facts/preferences, **grouped by the user they are about**.
  Returns nothing if it learned nothing new (short-circuits straight to END).
- ingest_node: writes each user's new facts into **Graphiti** (the same temporal
  knowledge graph the world-view pipeline uses, see app/services/worldview.py),
  one episode per fact under a per-user group_id. Graphiti performs its own
  entity/edge extraction, de-duplication, and temporal conflict resolution on
  ingest, so there is no LLM consolidation node anymore.
- profile_extraction_update_node: the fixed-slot-profile counterpart, running on
  a separate START branch in parallel. It extracts the structured profile slots
  for every participant and, in the same node, writes each user's slots back —
  the merge is a deterministic field-level overwrite (newer non-empty value
  wins), so it needs no consolidation pass or per-user fan-out. Profiles stay in
  the per-user ``user_facts_preferences.profile`` JSONB column in Postgres.

Retrieval of the free-form facts is via ``search_user_facts`` below (a
group-scoped Graphiti hybrid search), exposed to the context_fetcher node as the
``search_user_facts`` LangChain tool and threaded into the responder as
``{user_facts}`` (see app/services/llm.py).
"""

import asyncio
import traceback
from collections import defaultdict
from typing import Any, Dict, List
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
from pprint import pprint

import tiktoken

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, create_model
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import (
    USER_FACT_EXTRACTOR_SYSTEM_PROMPT,
    USER_PROFILE_EXTRACTOR_SYSTEM_PROMPT,
    USER_PROFILE_FIELDS,
)
from app.repository import (
    get_user_profile,
    set_user_profile,
)
from app.services.graphiti import ingest_facts, list_episodes, search_graph
from app.services.metrics import (
    LLM_CALLS,
    record_llm_empty_output,
    record_llm_error,
)

# NOTE: app.services.llm imports this module (for the search_user_facts tool),
# so the profile-cache helpers it provides (get_user_profiles_cached /
# update_user_profile_cache) are imported lazily inside
# profile_extraction_update_node to avoid a circular import at load time.

settings = get_settings()
BOT_NAME = settings.bot_name

# Per-user facts live in the same Neo4j database as the world view but under
# one Graphiti group_id per user, keeping each person's memory partition
# separate from the world view and from every other user.
_USER_FACTS_GROUP_PREFIX = "user-facts-"


def user_facts_group_id(user_id: int) -> str:
    """Graphiti group_id holding the personal-facts graph for one user."""
    return f"{_USER_FACTS_GROUP_PREFIX}{user_id}"

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))


def _tag(chat_id: int | None) -> str:
    """Log prefix for userfacts lines, with the chat id when we know it."""
    return f"[userfacts][{chat_id}]" if chat_id is not None else "[userfacts]"


def _profile_tag(chat_id: int | None) -> str:
    """Log prefix for the user_profile node, with the chat id when we know it."""
    return f"[userprofile][{chat_id}]" if chat_id is not None else "[userprofile]"

# Per-user lock guarding the read-modify-write in profile_extraction_update_node.
# Two conversations finishing at once that both involve the same user would
# otherwise race (both read the old profile, second write clobbers the first).
# Single-process/single-event-loop only — switch to a PG advisory lock if this
# ever runs across multiple workers.
_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# --- Storage access (Graphiti) ------------------------------------------------


async def search_user_facts(user_id: int, query: str | None = None) -> str:
    """Search one user's personal-facts partition; see ``graphiti.search_graph``."""
    return await search_graph(query, user_facts_group_id(user_id), "[userfacts]")


async def add_user_facts(user_id: int, facts: List[str]) -> None:
    """
    Setter method.
    Ingest new facts about one user into their Graphiti partition.

    One episode per fact; Graphiti's own dedup / temporal conflict resolution
    merges them against what is already stored. Used by the pipeline's
    ingest_node and by the admin surfaces (HTTP API + bot). Slow — each fact is
    several LLM round-trips (see graphiti.ingest_facts).
    """
    await ingest_facts(
        facts,
        group_id=user_facts_group_id(user_id),
        source_description="Rachel user fact",
        name_prefix=f"user-facts-{user_id}",
    )


async def get_user_facts(user_id: int) -> List[str]:
    """
    Getter method.
    Return every fact episode stored for one user, oldest first.

    Reads the raw ingested sentences straight off the user's partition — the
    full dump, not a search. Used by the admin surfaces (HTTP API + bot).
    """
    return await list_episodes(user_facts_group_id(user_id))


async def search_user_info(user_id: int, query: str | None = None) -> tuple[str, dict]:
    """Fetch BOTH halves of Rachel's memory about one participant in one shot.

    Wraps ``search_user_facts`` (the free-form Graphiti facts) and, in addition,
    pulls that user's fixed-slot structured profile via the responder's per-user
    cache (``get_user_profiles_cached``). The idea: whenever the context fetcher
    decides personal context about someone is worth looking up, we automatically
    grab their profile too, so both feed into the responder together.

    Returns ``(facts_text, profile_dict)`` — facts is the hybrid-search string
    (possibly empty), profile is the raw slot dict (``{}`` when nothing stored).
    """
    # Lazy import: app.services.llm imports this module for the tool, so a
    # top-level import here would be circular.
    from app.services.llm import get_user_profiles_cached

    facts = await search_user_facts(user_id, query)
    profiles = await get_user_profiles_cached([user_id])
    return facts, profiles.get(user_id, {})


@tool("search_user_info")
async def search_user_info_tool(name: str, query: str) -> str:
    """Recall what Rachel remembers about ONE conversation participant.

    Each participant has their own memory: durable personal facts and preferences
    learned from past conversations (relationships, plans, likes/dislikes, ongoing situations) plus a structured profile of core attributes.
    Use this to recall what Rachel already knows about a person so the responder can reply personally,
    e.g. when they mention something about their own life or when personal context would clearly help.

    Args:
        name: The participant's name, exactly as listed in the participants
            section of your instructions. Never invent a name.
        query: A focused, natural-language description of what to look up about
            this person (e.g. "job and internship plans", "food preferences").
    """
    # Name -> user_id resolution happens in the context_fetcher pipeline
    # (app.services.llm), which reads this tool call's args and invokes
    # search_user_info directly; this body is only a fallback for standalone use.
    return "Name-based lookup is resolved by the context fetcher pipeline."


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
    # Maps each sender name in the (whole) conversation to its numeric user_id.
    # Built at the entry point from the structured conversation — the only thing
    # still derived from it, since extraction_msgs (rendered strings) carry no ids
    # and resolution must cover people who spoke only in the context portion.
    name_to_id: Dict[str, int]
    # Narrative summary of the just-finished conversation, injected into the
    # extractor prompt as {chat_summary} to enrich extractions.
    summary: str
    # Pre-divided extraction history built once upstream (in
    # app/services/memory.py::update_memories): the conversation rendered as LLM
    # turns with the "already-processed" divider inserted. Both the facts and
    # profile branches read these directly.
    extraction_msgs: List[Any]
    # False when every message is at or below the watermark (nothing new to
    # extract), letting the facts branch short-circuit.
    has_new: bool
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


# --- Nodes -------------------------------------------------------------------


async def fact_extractor_node(state: UserFactsState) -> Dict:
    tag = _tag(state.get("chat_id"))
    # Only the sender NAME is shown to the model — it is far less likely to
    # hallucinate a name than a numeric id. We resolve names back to ids below
    # via name_to_id. extraction_msgs is pre-divided upstream: older
    # (already-processed) messages sit above the divider as context, only newer
    # ones below are to be extracted.
    if not state.get("has_new"):
        print(f"{tag} no new messages since last processed; skipping extraction")
        return {"extracted": {}}
    history_msgs = state["extraction_msgs"]

    now = datetime.now(SGT)
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
    LLM_CALLS.labels(node="userfacts_extractor").inc()
    try:
        result: ExtractorOutput = await _extractor_llm.ainvoke(msgs)
    except Exception as e:
        # A transient provider/transport error (e.g. an OpenRouter 403 error body)
        # must not abort the whole graph and discard the parallel profile branch —
        # skip fact extraction this round, same as a parse failure.
        kind = record_llm_error("userfacts_extractor", e)
        print(f"{tag} extractor LLM call failed ({kind}: {type(e).__name__}: {e}); skipping")
        return {"extracted": {}}

    if result is None:
        kind = record_llm_empty_output("userfacts_extractor")
        print(f"{tag} extractor returned None ({kind}: LLM parse failure); skipping")
        return {"extracted": {}}

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
        print(f"{tag} nothing to ingest (no resolvable new facts), ending")
    for uid, facts in extracted.items():
        print(f"{tag} extracted facts for user {uid}")
        pprint(facts)
    return {"extracted": extracted}


async def ingest_node(state: UserFactsState) -> Dict:
    """Write each user's new facts into their Graphiti partition.

    One episode per fact, under that user's group_id. All users' facts are
    ingested sequentially — ``ingest_facts`` serialises episode writes under the
    shared Graphiti lock anyway (concurrent add_episode calls race the shared
    graph), and Graphiti's own dedup / temporal conflict resolution replaces the
    old LLM consolidation pass entirely.
    """
    tag = _tag(state.get("chat_id"))
    for user_id, facts in state["extracted"].items():
        await add_user_facts(user_id, facts)
        print(f"{tag} ingested {len(facts)} fact(s) for user {user_id} into Graphiti")
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
    # Imported lazily: app.services.llm imports this module for the
    # search_user_facts tool, so a top-level import here would be circular.
    from app.services.llm import get_user_profiles_cached, update_user_profile_cache

    tag = _profile_tag(state.get("chat_id"))
    name_to_id = state["name_to_id"]
    # extraction_msgs is pre-divided upstream: older (already-processed) messages
    # sit above the divider as context, only newer ones below are to be extracted.
    # Short-circuit when there is nothing new so we neither re-derive profile slots
    # from already-processed messages nor spend an LLM call.
    if not state.get("has_new"):
        print(f"{tag} no new messages since last processed; skipping profile extraction")
        return {}
    history_msgs = state["extraction_msgs"]

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
    LLM_CALLS.labels(node="userfacts_profile").inc()
    try:
        result: ProfileExtractorOutput = await _profile_extractor_llm.ainvoke(msgs)
    except Exception as e:
        # A transient provider/transport error (e.g. an OpenRouter 403 error body)
        # must not abort the whole graph and discard the parallel fact branch —
        # skip the profile update this round, same as a parse failure.
        kind = record_llm_error("userfacts_profile", e)
        print(f"{tag} profile extractor LLM call failed ({kind}: {type(e).__name__}: {e}); skipping")
        return {}

    if result is None:
        kind = record_llm_empty_output("userfacts_profile")
        print(f"{tag} profile extractor returned None ({kind}: LLM parse failure); skipping")
        return {}

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


def _route_after_extraction(state: UserFactsState) -> str:
    """Skip ingestion when nothing new was learned about anyone."""
    return "ingest_node" if state["extracted"] else END


def _build_graph():
    graph: StateGraph = StateGraph(UserFactsState)
    graph.add_node("fact_extractor_node", fact_extractor_node)
    graph.add_node("ingest_node", ingest_node)
    graph.add_node("profile_extraction_update_node", profile_extraction_update_node)

    # Two independent branches fan out from START in parallel: free-form facts
    # and the fixed-slot profile. They read the same conversation but write to
    # different storage (per-user Graphiti partitions vs. profile JSONB), then
    # join at END. The facts branch mirrors the world-view pipeline: extract,
    # then ingest into Graphiti (which handles dedup/conflicts itself); the
    # profile branch does extraction + a deterministic field-level write in a
    # single node, so it needs no routing of its own.
    graph.add_edge(START, "fact_extractor_node")
    graph.add_conditional_edges(
        "fact_extractor_node",
        _route_after_extraction,
        {"ingest_node": "ingest_node", END: END},
    )
    graph.add_edge("ingest_node", END)

    graph.add_edge(START, "profile_extraction_update_node")
    graph.add_edge("profile_extraction_update_node", END)
    return graph.compile()


_graph = _build_graph()


async def update_user_facts(
    conversation: List[Dict[str, Any]],
    extraction_msgs: List[Any],
    has_new: bool,
    summary: str = "",
    chat_id: int | None = None,
) -> None:
    """Extract per-user facts from a finished conversation and ingest into Graphiti.

    ``extraction_msgs`` / ``has_new`` are the pre-divided extraction history built
    once upstream (app/services/memory.py::update_memories); both branches extract
    only from messages newer than the watermark. The raw ``conversation`` is used
    solely to build the name→id map (extraction_msgs carry no ids, and resolution
    must cover people who spoke only in the already-processed context portion).

    Also runs the parallel profile branch, which still writes to Postgres.
    Never raises — errors are caught and logged so memory upkeep can't crash the
    caller.
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
        "name_to_id": name_to_id,
        "summary": summary or "",
        "extraction_msgs": extraction_msgs,
        "has_new": has_new,
        "extracted": {},
        "chat_id": chat_id,
    }
    try:
        await _graph.ainvoke(state)
    except Exception as e:  # never let memory upkeep crash the caller
        print(f"{_tag(chat_id)} update failed: {e}")
        traceback.print_exc()
