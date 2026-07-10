"""LLM integration via LangGraph + LangChain OpenRouter.

The nodes are gated by a checker + router up front, then summarizer_node and
context_fetcher_node fan out in parallel before the responder joins them:

  START → checker_node → (must_reply?) → summarizer_node ──↘
                ↓ no                   → context_fetcher_node → responder_node → END
              router_node → (reply needed?) ──↗
                ↓ no
               END

checker_node:        cheap, no-LLM gate. If MUST_REPLY is set (1-on-1 chat or
                     Rachel was tagged), skip the router entirely and go straight
                     to the summarizer/context_fetcher. Otherwise defer to the
                     router.
router_node:         decides whether a reply is even warranted. If not, the
                     graph short-circuits straight to END (no summary, no
                     response, no context fetch).
summarizer_node:     detects conversation mood; result is stored in _chat_mood
                     and used by the NEXT call's responder_node. Runs in parallel
                     with context_fetcher_node.
context_fetcher_node: a single-pass tool-calling step whose sole job is to
                     decide which tools to call to gather extra context for the
                     responder: the calendar tools (Rachel's schedule), the
                     world-view search, and the per-user facts search (both
                     Graphiti). One LLM call picks the tools, they run once, and
                     the gathered context is written to state["schedule_context"]
                     / state["world_view"] / state["user_facts"] and injected
                     into the responder.
responder_node:      generates Rachel's reply using the mood detected in the
                     previous call (defaults to "default" on first contact) plus
                     whatever context_fetcher_node gathered this call.
"""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
from typing import Any, Dict, List, Optional, Tuple
from pprint import pprint

import tiktoken

from app.services import worldview
from app.services.metrics import LLM_CALLS, record_llm_error

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import (
    CONTEXT_FETCHER_SYSTEM_PROMPT,
    CONVERSATION_STYLE,
    ROUTER_PM_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    USER_PROFILE_ATTRIBUTE_GUIDE,
    USER_PROFILE_FIELDS,
)
from app.calander import CALENDAR_TOOLS
from app.services.userfacts import search_user_facts, search_user_info, search_user_info_tool
from app.services.worldview import search_world_view, search_worldview
from app.repository import (
    get_active_trait_prompts,
    get_responder_system_prompt,
    get_summarizer_system_prompt,
    get_user_profiles_batch,
)

settings = get_settings()
BOT_NAME = settings.bot_name

# The router only needs the tail of the conversation to decide whether a reply
# is warranted, so cap how many of the most recent buffer messages it sees.
ROUTER_CONTEXT_MSGS = 15

_chat_mood: Dict[int, str] = {}
DEFAULT_MOOD = "default"

# Rachel's own replies sit in the history we feed the graph (sender == BOT_NAME).
# To stop her re-answering messages she already handled, we partition the history
# at her *last* message: everything up to & including it is settled context, and
# only the messages after it are "new" and to be acted on. A divider message is
# inserted at that boundary in the router/context_fetcher/responder inputs (the
# summarizer keeps seeing the whole conversation, undivided).
ROUTER_NODE_DIVIDER = (
    "[EVERYTHING ABOVE IS EARLIER CONVERSATION, ALREADY HANDLED — CONTEXT ONLY. "
    "BASE YOUR DECISION ONLY ON THE MESSAGE(S) BELOW. IF THE MESSAGE(S) BELOW ARE "
    "UNCLEAR OR YOU DON'T KNOW WHAT THEY ARE RESPONDING TO, REFER TO THE "
    "CONVERSATION ABOVE FOR CONTEXT.]"
)
CONTEXT_NODE_DIVIDER = (
    "[EVERYTHING ABOVE IS EARLIER CONVERSATION, ALREADY HANDLED — CONTEXT ONLY. "
    "GATHER BACKGROUND NEEDED TO RESPOND TO THE NEW MESSAGE(S) BELOW. IF THE "
    "MESSAGE(S) BELOW ARE UNCLEAR OR YOU DON'T KNOW WHAT THEY ARE RESPONDING TO, "
    "REFER TO THE CONVERSATION ABOVE FOR CONTEXT.]"
)
RESPONDER_NODE_DIVIDER = (
    "[EVERYTHING ABOVE IS EARLIER CONVERSATION YOU HAVE ALREADY RESPONDED TO — "
    "CONTEXT ONLY. REPLY ONLY TO THE NEW MESSAGE(S) BELOW; DO NOT RE-ANSWER "
    "ANYTHING ABOVE. IF THE MESSAGE(S) BELOW ARE UNCLEAR OR YOU DON'T KNOW WHAT "
    "THEY ARE RESPONDING TO, REFER TO THE CONVERSATION ABOVE FOR CONTEXT.]"
)


def _partition_index(
    history: List[Dict[str, Any]], watermark: Optional[int] = None
) -> int:
    """First 'new' (not-yet-responded-to) index in ``history``.

    With a ``watermark`` (the highest telegram_message_id Rachel has already
    responded to) the boundary is the first *non-bot* message whose id exceeds
    it. This is more robust than keying off Rachel's last reply position: when a
    message arrives while Rachel is still generating a reply, her eventual reply
    gets a *higher* send-time id and so sorts to the buffer tail, hiding that
    newer-arrived (lower-id) message behind it — "one past Rachel's last message"
    would then wrongly treat the unanswered message as settled context.

    Without a watermark it falls back to one past Rachel's last message; 0 if she
    has none here. (The watermark path no longer uses a single index — see
    ``_is_new_message`` / ``_build_history_messages`` — because a message that is
    "new" by id but a bot turn must still render as context, which a single
    boundary can't express when the buffer is in send-time order.)
    """
    last_bot = -1
    for i, entry in enumerate(history):
        if entry["sender"] == BOT_NAME:
            last_bot = i
    return last_bot + 1


def _is_new_message(entry: Dict[str, Any], watermark: int) -> bool:
    """True when ``entry`` is an unanswered *user* message (a non-bot turn whose
    id exceeds the watermark). Rachel's own turns are never "new" — they're always
    already-said context, even when their send-time id sorts them past the
    watermark (which happens when the message that triggered them arrived mid-send)."""
    return entry.get("sender") != BOT_NAME and entry.get("telegram_message_id", 0) > watermark


def _build_history_messages(
    history: List[Dict[str, Any]],
    divider_text: Optional[str] = None,
    watermark: Optional[int] = None,
) -> List[Any]:
    """Render history as AIMessage/HumanMessage turns, optionally split by a divider.

    Messages are built as concrete Message objects (not templated tuples) so any
    literal '{' or '}' in user content passes through verbatim rather than being
    parsed as an f-string placeholder.

    When ``divider_text`` is given, a HumanMessage divider is placed between the
    already-handled context and the new messages to act on. With a ``watermark``,
    "new" means *unanswered user messages* (``_is_new_message``): every bot turn is
    rendered as context above the divider regardless of its send-time id, so
    Rachel's own last reply — which sorts to the tail when the triggering message
    arrived while she was still sending — can't slip below the divider and get
    re-generated. Without a watermark it falls back to the positional split at
    ``_partition_index`` (one past Rachel's last message).
    """
    def render(entry: Dict[str, Any]) -> Any:
        text = f"{entry['sender']}: {entry['content']}"
        return AIMessage(content=text) if entry["sender"] == BOT_NAME else HumanMessage(content=text)

    if divider_text is not None and watermark is not None:
        # Split into context vs. new, preserving each group's relative order. Bot
        # turns always land in context. Only emit a divider when both sides exist.
        context = [e for e in history if not _is_new_message(e, watermark)]
        new = [e for e in history if _is_new_message(e, watermark)]
        if context and new:
            return [render(e) for e in context] + [HumanMessage(content=divider_text)] + [render(e) for e in new]
        return [render(e) for e in history]

    idx = _partition_index(history, watermark)
    msgs: List[Any] = []
    for i, entry in enumerate(history):
        if divider_text is not None and i == idx and 0 < idx < len(history):
            msgs.append(HumanMessage(content=divider_text))
        msgs.append(render(entry))
    return msgs


def get_chat_mood(chat_id: int) -> str:
    """Return the last-detected mood for a chat (DEFAULT_MOOD if none)."""
    return _chat_mood.get(chat_id, DEFAULT_MOOD)


def set_chat_mood(chat_id: int, mood: str) -> None:
    """Seed the in-memory mood for a chat (e.g. restoring from DB on first load)."""
    _chat_mood[chat_id] = mood

# Structured profiles change only when the userfacts profile pipeline runs
# (once per finished conversation), so cache them per user_id with a short TTL
# instead of hitting the DB on every message. Worst case staleness is
# USER_FACTS_CACHE_TTL seconds. (Free-form facts now live in Graphiti and are
# fetched by the context_fetcher's search_user_facts tool — no cache here.)
USER_FACTS_CACHE_TTL = 5 * 60    # 5 min
_user_profile_cache: Dict[int, Tuple[dict, float]] = {}  # user_id -> (profile, fetched_at)


def update_user_profile_cache(user_id: int, profile: dict) -> None:
    """Write-through the profile cache after the userfacts profile pipeline runs,
    so the responder sees the freshly merged profile immediately."""
    _user_profile_cache[user_id] = (profile, time.monotonic())


async def _get_user_profiles_cached(user_ids: List[int]) -> Dict[int, dict]:
    """Return {user_id: profile_dict} for the given users, serving fresh cache
    hits and batching only the stale/missing ones into a single DB call."""
    now = time.monotonic()
    result: Dict[int, dict] = {}
    stale: List[int] = []
    for uid in user_ids:
        cached = _user_profile_cache.get(uid)
        if cached is not None and (now - cached[1]) < USER_FACTS_CACHE_TTL:
            result[uid] = cached[0]
        else:
            stale.append(uid)

    if stale:
        fetched = await get_user_profiles_batch(stale)
        for uid in stale:
            # Cache {} for users with no profile too, so we don't re-query them.
            profile = fetched.get(uid, {})
            _user_profile_cache[uid] = (profile, now)
            result[uid] = profile
    return result


async def get_user_profiles_cached(user_ids: List[int]) -> Dict[int, dict]:
    """Public accessor for the responder's profile cache: returns
    {user_id: profile_dict}, serving fresh cache hits and batching only the
    stale/missing users into a single DB call (populating the cache as it goes).
    Used by the userfacts profile pipeline for its context read."""
    return await _get_user_profiles_cached(user_ids)


def _render_profile(profile: dict, show_unknown: bool = False) -> str:
    """Render a stored profile dict as labelled lines, in schema order.

    With show_unknown=True, every slot is emitted — empty ones as "NIL" — so the
    responder can see which attributes are still gaps and subtly probe for them.
    With show_unknown=False, empty slots are skipped. Returns "" if nothing to show.
    """
    lines = []
    for key, label, _guide in USER_PROFILE_FIELDS:
        value = str(profile.get(key, "")).strip()
        if value:
            lines.append(f"- {label}: {value}")
        elif show_unknown:
            lines.append(f"- {label}: NIL")
    return "\n".join(lines)

# Mood labels are defined by CONVERSATION_STYLE's keys, not a static
# Literal — Field(json_schema_extra={"enum": ...}) lets the structured-output
# schema track that dict dynamically.
MOOD_LABELS = list(CONVERSATION_STYLE)


class RouterOutput(BaseModel):
    # should_reply is declared first so the model commits to the decision before
    # writing the (cosmetic) justification — it sometimes drops trailing fields.
    should_reply: bool = Field(
        ...,
        description="True if Rachel should send a reply to the latest messages, False if she should stay silent.",
    )
    reason: str = Field(
        ...,
        description="A single short sentence explaining the reply/no-reply decision.",
    )


class SummarizerOutput(BaseModel):
    summary: str = Field(
        ...,
        description='New word summary of the conversation of 200 words maximum, or the exact string "NIL" if the old summary is still sufficient.',
    )
    mood: str = Field(
        ...,
        description="The detected conversational mood.",
        json_schema_extra={"enum": MOOD_LABELS},
    )


class ResponseOutput(BaseModel):
    reason: str = Field(
        ...,
        description="A single sentence explaining why this reply was given, naming which part of the personality traits or system prompt instructions drove it. For traceability and debugging.",
    )
    content: str = Field(
        ...,
        description="Rachel's reply as plain text in her natural Singlish voice. Use \\n\\n to separate message bursts. Do NOT include her name or any prefix.",
    )


class GraphState(TypedDict):
    history: List[Dict[str, Any]]
    current_summary: str | None
    mood: str
    senders: Dict[int, str]  # sender_user_id -> display name
    must_reply: bool  # set by caller: Rachel was tagged/replied-to in a group
    is_private: bool  # set by caller: 1-on-1 DM (selects the PM router prompt)
    # Highest telegram_message_id Rachel has already responded to; drives the
    # "already handled vs. new" divider (see _partition_index). None on first contact.
    responded_watermark: Optional[int]
    should_reply: bool
    schedule_context: str  # extra schedule context gathered by context_fetcher_node
    world_view: str  # world-view facts gathered by context_fetcher_node
    user_facts: str  # per-user facts gathered by context_fetcher_node (Graphiti)
    user_profiles: str  # per-user structured profiles gathered by context_fetcher_node
    response_text: str
    response_reason: str


# include_raw lets router_node salvage should_reply from the raw tool-call args
# when structured parsing fails (the model occasionally omits a required field).
_router_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.0,
).with_structured_output(RouterOutput, include_raw=True)

_summarizer_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.0,
).with_structured_output(SummarizerOutput)

# json_mode avoids tool-calling, which hangs on this model.
_responder_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.2,
).with_structured_output(ResponseOutput)

# The context_fetcher is the one node that *does* use tool-calling: it has these
# tools bound and decides which to call. (CLAUDE.md notes tool-calling can hang on
# some models — the node is wrapped in a hard timeout below and fails open to empty
# context, so a hang/error can never stall the pipeline.) CONTEXT_TOOLS is the
# single source of truth: it drives both the bound tools and the prompt's {tools}
# listing (via format_tools). It combines the calendar tools with the world-view
# search tool (Rachel's general learned facts).
CONTEXT_TOOLS = [*CALENDAR_TOOLS, search_world_view, search_user_info_tool]


def format_tools(tools) -> str:
    """Render any list of LangChain tools as a bullet list for a prompt.

    Derived entirely from each tool's name, argument names and FULL docstring, so
    the prompt never hard-codes tool descriptions — the tools stay the one source
    of truth. Each entry is ``- name(args):`` followed by the complete docstring.
    """
    lines = []
    for t in tools:
        args = ", ".join(t.args.keys())
        doc = (t.description or "").strip()
        lines.append(f"- {t.name}({args}):\n{doc}")
    return "\n\n".join(lines)


_context_tools_by_name = {t.name: t for t in CONTEXT_TOOLS}
_context_fetcher_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.0,
).bind_tools(CONTEXT_TOOLS)

# The context_fetcher is a single pass (one LLM call → run whatever tools it
# asked for → done), so the only guard needed is a wall-clock timeout.
CONTEXT_FETCHER_TIMEOUT = 30  # seconds


def checker_node(state: GraphState) -> Dict:
    """Cheap, no-LLM gate in front of the router. Does nothing on its own; the
    routing decision lives in _route_after_checker, which reads must_reply."""
    return {}


def _has_new_messages(state: GraphState) -> bool:
    """True when the history holds at least one unanswered user message (a non-bot
    turn past the watermark). With no watermark (first contact) everything counts
    as new. Mirrors the divider logic in _build_history_messages so the graph and
    the client agree on what 'nothing to reply to' means."""
    watermark = state.get("responded_watermark")
    if watermark is None:
        return True
    return any(_is_new_message(e, watermark) for e in state["history"])


def _route_after_checker(state: GraphState):
    """Conditional edge: if the caller forced a reply (1-on-1 chat or Rachel was
    tagged), skip the router and fan out to the summarizer + context_fetcher.
    Otherwise let the router decide whether a reply is warranted."""
    # "Nothing new" overrides everything, including must_reply: if there is nothing
    # unanswered since the watermark there is nothing to reply to, and running on a
    # divider-less flat transcript would only risk a repeat (defense-in-depth for
    # the client's shield-race redundant reply). Short-circuit to END.
    if not _has_new_messages(state):
        return END
    if state.get("must_reply", False):
        return ["summarizer_node", "context_fetcher_node"]
    return "router_node"


async def router_node(state: GraphState) -> Dict:
    """Decide whether Rachel should reply at all. On failure, default to replying
    (fail-open) so a flaky router never silences her."""
    # Only show the router the most recent messages — it just needs the latest
    # context to judge whether a reply is warranted, not the whole buffer.
    recent_history = state["history"][-ROUTER_CONTEXT_MSGS:]
    history_msgs = _build_history_messages(
        recent_history, divider_text=ROUTER_NODE_DIVIDER, watermark=state.get("responded_watermark")
    )

    # In a 1-on-1 DM use the PM-specific gate (filters out low-information
    # acknowledgements and already-answered messages); in groups use the default
    # gate (filters out chatter that doesn't concern Rachel).
    router_prompt = ROUTER_PM_SYSTEM_PROMPT if state.get("is_private") else ROUTER_SYSTEM_PROMPT
    system_msgs = ChatPromptTemplate.from_messages(
        [("system", router_prompt)]
    ).format_messages(current_summary=state.get("current_summary") or "")
    msgs = [*system_msgs, *history_msgs]

    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"[router] context: {len(msgs)} messages, {msgs_tokens} tokens")

    LLM_CALLS.labels(node="router").inc()
    try:
        result = await _router_llm.ainvoke(msgs)
    except Exception as e:
        kind = record_llm_error("router", e)
        print(f"[router] LLM error ({kind}; defaulting to reply): {type(e).__name__}: {e}")
        return {"should_reply": True}

    parsed: RouterOutput | None = result.get("parsed")
    if parsed is not None:
        print(f"[router] should_reply={parsed.should_reply} | reason={parsed.reason}")
        return {"should_reply": parsed.should_reply}

    # Structured parsing failed (model omitted a required field). Try to salvage
    # the actual decision from the raw tool-call args before falling open — a
    # dropped `reason` shouldn't force a reply when should_reply was given.
    should_reply = _salvage_should_reply(result.get("raw"))
    print(
        f"[router] structured parse failed ({result.get('parsing_error')}); "
        f"salvaged should_reply={should_reply}"
    )
    return {"should_reply": should_reply}


def _salvage_should_reply(raw: Any) -> bool:
    """Pull should_reply out of a raw model response whose structured parse failed.
    Falls open to True (reply) when the field is absent or unreadable."""
    try:
        tool_calls = getattr(raw, "tool_calls", None) or []
        if tool_calls:
            value = tool_calls[0].get("args", {}).get("should_reply")
            if isinstance(value, bool):
                return value
    except Exception:
        pass
    return True


def _route_after_router(state: GraphState):
    """Conditional edge: fan out to the summarizer + context_fetcher only if a
    reply is wanted; otherwise short-circuit straight to END."""
    if state.get("should_reply", True):
        return ["summarizer_node", "context_fetcher_node"]
    return END


async def summarizer_node(state: GraphState) -> Dict:
    # The summarizer summarizes the WHOLE conversation, so no divider — it sees the
    # full, undivided history.
    history_msgs = _build_history_messages(state["history"])

    system_prompt_str = await get_summarizer_system_prompt()

    system_msgs = ChatPromptTemplate.from_messages(
        [("system", system_prompt_str)]
    ).format_messages(
        mood_list=MOOD_LABELS,
        old_summary=state.get("current_summary") or "")
    msgs = [*system_msgs, *history_msgs]

    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"[summarizer] context: {len(msgs)} messages, {msgs_tokens} tokens")

    LLM_CALLS.labels(node="summarizer").inc()
    try:
        result: SummarizerOutput = await _summarizer_llm.ainvoke(msgs)
    except Exception as e:
        kind = record_llm_error("summarizer", e)
        print(f"[summarizer] LLM error ({kind}; keeping current mood/summary): {type(e).__name__}: {e}")
        return {}
    print(f"Detected mood: {result.mood} | Summary: {result.summary}")
    if result.summary == "NIL":
        return {"mood": result.mood}
    return {"mood": result.mood, "current_summary": result.summary}


def _normalize_name(name: str) -> str:
    """Reduce a name to a stripped, lowercase, alphanumeric-only key so the
    model's loosely-typed name output can be matched against the participant
    list regardless of whitespace, casing, or punctuation."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _build_name_to_id(senders: Dict[int, str]) -> Dict[str, Optional[int]]:
    """Map each participant's normalized name to their user_id. If two
    participants normalize to the same key, that key is marked ambiguous
    (mapped to ``None``) so it can never resolve to a wrong user."""
    mapping: Dict[str, Optional[int]] = {}
    for uid, name in senders.items():
        key = _normalize_name(name)
        if not key:
            continue
        if key in mapping and mapping[key] != uid:
            mapping[key] = None  # ambiguous: two senders share this normalized name
        else:
            mapping[key] = uid
    return mapping


def _resolve_name_to_id(
    raw_name: str, name_to_id: Dict[str, Optional[int]]
) -> Optional[int]:
    """Clean the model's raw name output and return the unique matching
    user_id, or ``None`` if there's no unambiguous match."""
    return name_to_id.get(_normalize_name(raw_name))


async def _run_context_fetcher(state: GraphState) -> Tuple[str, str, str, str]:
    """Single pass for context_fetcher_node: one LLM call decides which tools to
    call, then those tools are run once. Factored out so the node can wrap it in
    a single asyncio timeout. Returns ``(schedule_context, world_view,
    user_facts, user_profiles)`` — all empty if the model asked for no tools.

    The world-view and per-user tools' outputs are routed to their own return
    slots (so they land in the responder's ``{world_view}`` / ``{user_facts}`` /
    ``{user_profiles}`` sections, not ``{schedule_context}``) — looking up a user
    yields both their facts and their profile; all other (calendar) tools are
    concatenated into the schedule context."""
    senders = state.get("senders") or {}
    history_msgs = _build_history_messages(
        state["history"], divider_text=CONTEXT_NODE_DIVIDER, watermark=state.get("responded_watermark")
    )

    now = datetime.now(SGT)
    formatted_datetime = (
        f"The current date is {now.strftime('%d %B %Y')}, "
        f"the current day of week is {now.strftime('%A')}, "
        f"the current time is {now.strftime('%H:%M')}"
    )
    participants = ", ".join(senders.values()) or "(unknown)"
    # Normalized-name -> user_id map used to resolve the model's name output back
    # to a concrete id downstream (the model only ever sees/emits names).
    name_to_id = _build_name_to_id(senders)
    system_msgs = ChatPromptTemplate.from_messages(
        [("system", CONTEXT_FETCHER_SYSTEM_PROMPT)]
    ).format_messages(
        datetime=formatted_datetime,
        tools=format_tools(CONTEXT_TOOLS),
        participants=participants,
    )
    msgs = [*system_msgs, *history_msgs]

    ai: AIMessage = await _context_fetcher_llm.ainvoke(msgs)
    tool_calls = getattr(ai, "tool_calls", None) or []
    print(f"[context_fetcher] LLM requested {len(tool_calls)} tool call(s):")
    for tc in tool_calls:
        print(f"[context_fetcher]   -> {tc['name']}({tc['args']})")
    if not tool_calls:
        return "", "", "", ""

    collected: List[str] = []
    world_view_parts: List[str] = []
    user_facts_parts: List[str] = []
    user_profile_parts: List[str] = []
    for tc in tool_calls:
        if tc["name"] == search_world_view.name:
            # World-view output goes to its own slot (the responder's {world_view}
            # section). Fails open to no facts, never crashes the pass.
            query = tc["args"].get("query", "")
            try:
                result = await search_worldview(query)
            except Exception as e:
                print(f"[context_fetcher] world-view search error: {type(e).__name__}: {e}")
                result = ""
            print(f"[context_fetcher] {tc['name']}({tc['args']}) result:\n{result}")
            if result:
                world_view_parts.append(result)
            continue

        if tc["name"] == search_user_info_tool.name:
            # Looking up a user pulls BOTH their free-form facts and their
            # structured profile (search_user_info) — facts go to the responder's
            # {user_facts} slot, profile to {user_profiles}, both labelled per
            # participant. Fails open, never crashes.
            raw_name = tc["args"].get("name", "")
            user_id = _resolve_name_to_id(raw_name, name_to_id)
            if user_id is None:
                print(f"[context_fetcher] user-info search skipped (unresolved name: {tc['args']})")
                continue
            query = tc["args"].get("query", "")
            try:
                facts, profile = await search_user_info(user_id, query)
            except Exception as e:
                print(f"[context_fetcher] user-info search error: {type(e).__name__}: {e}")
                facts, profile = "", {}
            name = (senders.get(user_id) or "").strip()
            label = f"{name} (id {user_id})" if name else f"User {user_id}"
            print(f"[context_fetcher] {tc['name']}({tc['args']}) facts:\n{facts}")
            if facts:
                user_facts_parts.append(f"{label}:\n{facts}")
            # Show every slot (NIL for empties) so the responder sees which
            # attributes are still gaps to subtly probe.
            rendered_profile = _render_profile(profile, show_unknown=True)
            print(f"[context_fetcher] {tc['name']}({tc['args']}) profile:\n{rendered_profile}")
            if rendered_profile:
                user_profile_parts.append(f"{label}:\n{rendered_profile}")
            continue

        tool = _context_tools_by_name.get(tc["name"])
        if tool is None:
            result = f"Unknown tool '{tc['name']}'."
        else:
            try:
                result = await tool.ainvoke(tc["args"])
            except Exception as e:
                result = f"tool error: {type(e).__name__}: {e}"
        print(f"[context_fetcher] {tc['name']}({tc['args']}) result:\n{result}")
        collected.append(str(result))

    schedule_context = "\n\n".join(collected)
    world_view = "\n".join(world_view_parts)
    user_facts = "\n\n".join(user_facts_parts)
    user_profiles = "\n\n".join(user_profile_parts)
    return schedule_context, world_view, user_facts, user_profiles


async def context_fetcher_node(state: GraphState) -> Dict:
    """Gather extra context for the responder by calling the right tools.

    Runs in parallel with summarizer_node. Never raises and never stalls the
    pipeline: on any error or timeout it falls open to empty context (the
    responder still has the mood/summary and can reply without extras)."""
    LLM_CALLS.labels(node="context_fetcher").inc()
    try:
        schedule_context, world_view, user_facts, user_profiles = await asyncio.wait_for(
            _run_context_fetcher(state), timeout=CONTEXT_FETCHER_TIMEOUT
        )
    except asyncio.TimeoutError as e:
        record_llm_error("context_fetcher", e)
        print(f"[context_fetcher] timed out after {CONTEXT_FETCHER_TIMEOUT}s (no extra context)")
        return {"schedule_context": "", "world_view": "", "user_facts": "", "user_profiles": ""}
    except Exception as e:
        kind = record_llm_error("context_fetcher", e)
        print(f"[context_fetcher] error ({kind}; no extra context): {type(e).__name__}: {e}")
        return {"schedule_context": "", "world_view": "", "user_facts": "", "user_profiles": ""}

    print(
        f"[context_fetcher] gathered {_count_tokens(schedule_context)} tokens of "
        f"schedule context + {_count_tokens(world_view)} tokens of world-view facts "
        f"+ {_count_tokens(user_facts)} tokens of user facts "
        f"+ {_count_tokens(user_profiles)} tokens of user profiles"
    )
    return {
        "schedule_context": schedule_context,
        "world_view": world_view,
        "user_facts": user_facts,
        "user_profiles": user_profiles,
    }


async def responder_node(state: GraphState) -> Dict:
    try:


        system_prompt_str = await get_responder_system_prompt()
        print(f"[responder] system prompt fetched (tokens={_count_tokens(system_prompt_str)})")
    

        personality_traits = await get_active_trait_prompts()
        print(f"[responder] personality traits fetched (tokens={_count_tokens(personality_traits)})")

        mood = state.get("mood", "default")
        communication_style = CONVERSATION_STYLE.get(mood, CONVERSATION_STYLE["default"])
        print(f"[responder] communication style fetched (tokens={_count_tokens(communication_style)})")

        # World-view facts are fetched by context_fetcher_node (which generates a
        # conversation-aware query) and arrive as a bulleted string in state.
        world_view = state.get("world_view") or "Nothing learned yet."
        print(f"[responder] world view from state (tokens={_count_tokens(world_view)})")


        # Both the free-form facts and the structured profiles are fetched by
        # context_fetcher_node (its search_user_info tool pulls both halves for
        # each participant it decides to look up) and arrive in state — the
        # responder no longer reads profiles itself.
        user_facts = state.get("user_facts") or "Nothing learned yet."
        user_profiles = state.get("user_profiles") or "No one looked up yet."
        print(
            f"[responder] user memory from state "
            f"(facts_tokens={_count_tokens(user_facts)}, profile_tokens={_count_tokens(user_profiles)})"
        )

        now = datetime.now(SGT)
        formatted_datetime = (
            f"The current date is {now.strftime('%d %B %Y')}, "
            f"the current month is {now.strftime('%B')}, "
            f"the current day of week is {now.strftime('%A')}, "
            f"the current time is {now.strftime('%H:%M')}"
        )
        # Rachel's schedule (right now / today / other days) is no longer
        # injected here — context_fetcher_node fetches it via tools and it
        # arrives through {schedule_context}.

        history_msgs = _build_history_messages(
            state["history"], divider_text=RESPONDER_NODE_DIVIDER, watermark=state.get("responded_watermark")
        )
        history_tokens = sum(_count_tokens(f"{m.type}: {m.content}") for m in history_msgs)
        print(f"[responder] history fetched (count={len(history_msgs)}, tokens={history_tokens})")
        pprint(history_msgs)

        system_tmpl = ChatPromptTemplate.from_messages([("system", system_prompt_str)])

        system_msgs = system_tmpl.format_messages(
            communication_style=communication_style,
            current_summary=state.get("current_summary") or "",
            personality_traits=personality_traits,
            conversation_mood=mood,
            datetime=formatted_datetime,
            schedule_context=state.get("schedule_context") or "No schedule context was fetched.",
            world_view=world_view,
            user_facts=user_facts,
            user_profiles=user_profiles,
            profile_attributes=USER_PROFILE_ATTRIBUTE_GUIDE,
        )
        msgs = [*system_msgs, *history_msgs]
        msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
        print(f"[responder] TOTAL context: {len(msgs)} messages, {msgs_tokens} tokens")

        LLM_CALLS.labels(node="responder").inc()
        try:
            result: ResponseOutput = await _responder_llm.ainvoke(msgs)
        except OutputParserException as e:
            # The model sometimes ignores json_mode and returns Rachel's reply as
            # plain prose. The raw text is still a usable reply, so salvage it from
            # the exception rather than crashing the whole reply task.
            raw = (e.llm_output or "").strip()
            prefix = f"{BOT_NAME}:"
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
            if not raw:
                raise
            record_llm_error("responder", e)
            print(f"[responder] json parse failed; salvaging raw text ({len(raw)} chars)")
            return {
                "response_text": raw,
                "response_reason": "Salvaged from non-JSON model output (json_mode parse failure).",
            }
        print(f"[responder] LLM returned: content={result.content[:80]!r} | reason={result.reason!r}")
        return {"response_text": result.content, "response_reason": result.reason}
    except BaseException as e:
        import traceback
        # Don't count cooperative cancellation (a racing new message cancels the
        # shielded reply task) as an LLM failure — only real errors.
        if not isinstance(e, asyncio.CancelledError):
            record_llm_error("responder", e)
        print(f"[responder] EXCEPTION {type(e).__name__}: {e}")
        traceback.print_exc()
        raise


def _build_graph():
    graph: StateGraph = StateGraph(GraphState)
    graph.add_node("checker_node", checker_node)
    graph.add_node("router_node", router_node)
    graph.add_node("summarizer_node", summarizer_node)
    graph.add_node("context_fetcher_node", context_fetcher_node)
    graph.add_node("responder_node", responder_node)
    graph.add_edge(START, "checker_node")
    # Checker gates the router: a forced reply (must_reply) fans out straight to
    # the summarizer + context_fetcher; otherwise the router decides first.
    graph.add_conditional_edges(
        "checker_node",
        _route_after_checker,
        {
            "summarizer_node": "summarizer_node",
            "context_fetcher_node": "context_fetcher_node",
            "router_node": "router_node",
            END: END,
        },
    )
    # Router gates everything: if no reply is warranted, jump straight to END.
    graph.add_conditional_edges(
        "router_node",
        _route_after_router,
        {
            "summarizer_node": "summarizer_node",
            "context_fetcher_node": "context_fetcher_node",
            END: END,
        },
    )
    # Summarizer + context_fetcher run in parallel; the responder joins them
    # (LangGraph waits for both before running responder_node once).
    graph.add_edge("summarizer_node", "responder_node")
    graph.add_edge("context_fetcher_node", "responder_node")
    graph.add_edge("responder_node", END)
    return graph.compile()


_graph = _build_graph()


async def get_response(
    history: List[Dict[str, str]],
    current_summary: str | None = None,
    chat_id: int | None = None,
    senders: Dict[int, str] | None = None,
    must_reply: bool = False,
    is_private: bool = False,
    responded_watermark: int | None = None,
) -> Tuple[str, str, str | None, float]:
    """Run the LangGraph pipeline and return ``(response_text, reason, new_summary, elapsed_seconds)``.

    When ``must_reply`` is True (Rachel was tagged/replied-to in a group) the
    checker skips the router and a reply is always produced. Otherwise the router
    decides whether a reply is warranted — using the PM-specific gate when
    ``is_private`` is True (1-on-1 DM) or the default group gate otherwise. If no
    reply is warranted, the graph short-circuits and
    ``response_text`` comes back empty (no message is sent). When a reply is
    produced, summarizer_node runs before responder_node. The mood detected this
    call is stored in _chat_mood and injected on the *next* call.
    """
    start = time.time()



    current_mood = _chat_mood.get(chat_id, DEFAULT_MOOD) if chat_id is not None else DEFAULT_MOOD

    initial_state: GraphState = {
        "history": history,
        "current_summary": current_summary,
        "mood": current_mood,
        "senders": senders or {},
        "must_reply": must_reply,
        "is_private": is_private,
        "responded_watermark": responded_watermark,
        "should_reply": True,
        "schedule_context": "",
        "world_view": "",
        "user_facts": "",
        "response_text": "",
        "response_reason": "",
    }

    result = await _graph.ainvoke(initial_state)

    if chat_id is not None:
        _chat_mood[chat_id] = result["mood"]
        print(f"[{chat_id}] Stored mood for next call: {result['mood']}")

    new_summary = result["current_summary"] if result["current_summary"] != current_summary else None
    return result["response_text"], result["response_reason"], new_summary, time.time() - start
