"""Bounded context–responder loop over Merge Gateway and LangGraph.

The checker/router gates reply generation. On admission, summarization and the
initial context pass run in parallel. The responder joins them, evaluates all
raw observations, and either replies or requests another context pass. Only the
fetcher executes tools; the summarizer stays outside the loop. Pass and
remaining-step guards stop retrieval while preserving a best-effort reply.
"""

import asyncio
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
from typing import Any, Dict, List, Literal, Optional, Tuple
from pprint import pprint

import tiktoken
import httpx

from app.services import worldview
from app.services.metrics import AGENT_CONTEXT_PASSES, AGENT_LOOP_OUTCOMES, LLM_CALLS, record_llm_error
from app.services.context import ToolObservation
from app.services.prompt_builders import (
    build_context_fetcher_handoff,
    build_context_fetcher_prompt,
    build_proactive_trigger,
    build_responder_context,
    build_responder_loop_message,
    build_responder_prompt,
)
from app.services.tracing import traced_jev_decision

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))

from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphRecursionError
from langgraph.managed import RemainingSteps
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field, ConfigDict, ValidationError
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import (
    CONTEXT_FETCHER_SYSTEM_PROMPT,
    RESPONDER_LOOP_INSTRUCTIONS,
    RESPONDER_SYSTEM_PROMPT,
    SUMMARIZER_SYSTEM_PROMPT,
    CONVERSATION_STYLE,
    ROUTER_GROUP_QUESTION,
    ROUTER_PRIVATE_QUESTION,
)
from app.calander import CALENDAR_TOOLS
from app.services.proactive_tools import PROACTIVE_TOOLS
from app.services.userfacts import search_user_info, search_user_info_tool
from app.services.worldview import search_world_view, search_worldview
from app.repository import (
    get_active_models,
    get_active_trait_prompts,
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


# Mood labels are defined by CONVERSATION_STYLE's keys, not a static
# Literal — Field(json_schema_extra={"enum": ...}) lets the structured-output
# schema track that dict dynamically.
MOOD_LABELS = list(CONVERSATION_STYLE)


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
    decision: Literal["respond", "fetch_more"] = Field(
        ..., description="Respond now, or request one more supported context/schedule-action pass."
    )
    retrieval_request: str = Field(
        ..., description="Missing information or desired proactive schedule action for the context fetcher; empty for respond. Do not select tools."
    )
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(
        ...,
        description="A single sentence explaining why this reply was given, naming which part of the personality traits or system prompt instructions drove it. For traceability and debugging.",
    )
    content: str = Field(
        ...,
        description="Rachel's reply as plain text in her natural Singlish voice. Always required: for fetch_more, provide a best-effort clarification acknowledging uncertainty and asking one focused question. Use \\n\\n to separate message bursts. Do NOT include her name or any prefix.",
    )


class GraphState(TypedDict):
    history: List[Dict[str, Any]]
    chat_id: int | None
    intent: str
    current_summary: str | None
    mood: str
    senders: Dict[int, str]  # sender_user_id -> display name
    must_reply: bool  # set by caller: Rachel was tagged/replied-to in a group
    is_private: bool  # set by caller: 1-on-1 DM (selects the PM router prompt)
    # Highest telegram_message_id Rachel has already responded to; drives the
    # "already handled vs. new" divider (see _partition_index). None on first contact.
    responded_watermark: Optional[int]
    should_reply: bool
    context_passes: int
    retrieval_request: str
    tool_observations: List[ToolObservation]
    remaining_steps: RemainingSteps
    responder_decision: Literal["respond", "fetch_more"]
    responder_status: Literal["evaluated", "parse_fallback"]
    loop_outcome: Literal["first_pass", "retried", "capped"]
    response_text: str
    response_reason: str




#
# The chat clients are built lazily and cached keyed on the active
# "main" model string (from the DB, via repository.get_active_models), so an admin
# switching the model at runtime takes effect on the next node call without a
# restart. invalidate_model_clients() clears this cache (and resets Graphiti's
# singleton) when the active model changes.
#
_client_cache: Dict[str, Any] = {}


async def _current_main_model() -> str:
    """The active 'main' model string, falling back to config on any error."""
    try:
        return (await get_active_models())["main"]
    except Exception:
        return settings.llm_model


def _base_chat(model: str, temperature: float, reasoning_effort: str = "none") -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=settings.merge_gateway_api_key,
        base_url=settings.merge_gateway_openai_base_url,
        temperature=temperature,
        # Tool-bearing clients keep reasoning disabled because persisted replies
        # contain only visible text, not reasoning_content to replay with tools.
        reasoning_effort=reasoning_effort,
    )


async def _get_summarizer_llm():
    model = await _current_main_model()
    key = f"summarizer:{model}"
    if key not in _client_cache:
        _client_cache[key] = _base_chat(model, 0.0).with_structured_output(
            SummarizerOutput, method="function_calling"
        )
    return _client_cache[key]


async def _get_responder_llm():
    model = await _current_main_model()
    key = f"responder:{model}"
    if key not in _client_cache:
        _client_cache[key] = _base_chat(model, 0.2, reasoning_effort="medium").with_structured_output(
            # Validate locally so an SDK-side schema error cannot discard the
            # visible reply before our fallback gets a chance to salvage it.
            ResponseOutput.model_json_schema(), method="json_schema", strict=True, include_raw=True
        )
    return _client_cache[key]

# The context_fetcher is the one node that *does* use tool-calling: it has these
# tools bound and decides which to call. (CLAUDE.md notes tool-calling can hang on
# some models — each pass has a hard timeout below and records failures as
# observations, so a hang/error can never stall the pipeline.) CONTEXT_TOOLS is the
# single source of truth for both the agent's tools and its dynamic prompt.
CONTEXT_TOOLS = [*CALENDAR_TOOLS, search_world_view, search_user_info_tool, *PROACTIVE_TOOLS]


class ContextFetcherPromptContext(TypedDict):
    participants: str
    datetime: str
    intent: str


@dynamic_prompt
def context_fetcher_dynamic_prompt(request: ModelRequest) -> str:
    """Supply guidance only for the tools available on this model call."""
    tools = request.tools or []
    context: ContextFetcherPromptContext = request.runtime.context
    return build_context_fetcher_prompt(
        system_prompt=CONTEXT_FETCHER_SYSTEM_PROMPT,
        tools=tools,
        available_categories={_context_category(tool.name) for tool in tools},
        participants=context["participants"],
        current_datetime=context["datetime"],
        intent=context.get("intent", ""),
    )


_context_tools_by_name = {t.name: t for t in CONTEXT_TOOLS}


async def _get_context_fetcher_llm():
    model = await _current_main_model()
    key = f"context_fetcher:{model}"
    if key not in _client_cache:
        _client_cache[key] = _base_chat(model, 0.0, reasoning_effort="medium")
    return _client_cache[key]


def invalidate_model_clients() -> None:
    """Clear the reply-pipeline client cache and reset dependent singletons so
    the next node call rebuilds against the freshly-switched active model.

    Called by repository._invalidate_active_models_cache() after a model switch.
    Also resets the world-view / user-facts extractor clients and the Graphiti
    client (all keyed on the same 'main' model)."""
    _client_cache.clear()
    # Extractor clients live in their own modules; reset them if importable.
    for mod_name, reset_attr in (
        ("app.services.worldview", "reset_extractor_clients"),
        ("app.services.userfacts", "reset_extractor_clients"),
        ("app.services.graphiti", "reset_graphiti"),
    ):
        try:
            import importlib

            reset = getattr(importlib.import_module(mod_name), reset_attr, None)
            if reset is not None:
                reset()
        except Exception:
            pass

# Each context-fetcher invocation is one pass with one shared deadline for
# tool selection and execution. Earlier completed results survive the deadline.
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

    question = ROUTER_PRIVATE_QUESTION if state.get("is_private") else ROUTER_GROUP_QUESTION
    decision_state = (
        f"Conversation summary: {state.get('current_summary') or '(none)'}\n\n"
        "Recent messages:\n"
        + "\n".join(str(message.content) for message in history_msgs)
    )
    print(f"[router] context: {len(history_msgs)} messages, {_count_tokens(decision_state)} tokens")

    LLM_CALLS.labels(node="router").inc()
    try:
        with traced_jev_decision(settings.decisions_llm, decision_state, question) as record_result:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{settings.merge_gateway_base_url.rstrip('/')}/decisions",
                    headers={"Authorization": f"Bearer {settings.merge_gateway_api_key}"},
                    json={
                        "model": settings.decisions_llm,
                        "state": decision_state,
                        "questions": {"should_reply": question},
                    },
                )
                response.raise_for_status()
            decision = response.json()
            answer = decision["answers"]["should_reply"]
            probability = answer["noul"]
            if (
                answer.get("type") != "noul"
                or isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not math.isfinite(probability)
                or not 0 <= probability <= 1
            ):
                raise ValueError("Invalid should_reply Noul answer")
            should_reply = probability >= 0.8
            record_result(decision, probability, should_reply)
    except Exception as e:
        kind = record_llm_error("router", e)
        print(f"[router] decision error ({kind}; defaulting to reply): {type(e).__name__}: {e}")
        return {"should_reply": True}

    print(f"[router] should_reply={should_reply} | probability={probability:.3f}")
    return {"should_reply": should_reply}


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

    system_msgs = ChatPromptTemplate.from_messages(
        [("system", SUMMARIZER_SYSTEM_PROMPT)]
    ).format_messages(
        mood_list=MOOD_LABELS,
        old_summary=state.get("current_summary") or "")
    msgs = [*system_msgs, *history_msgs]

    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"[summarizer] context: {len(msgs)} messages, {msgs_tokens} tokens")

    LLM_CALLS.labels(node="summarizer").inc()
    try:
        result: SummarizerOutput = await (await _get_summarizer_llm()).ainvoke(msgs)
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


def _context_category(name: str) -> str:
    if name in {tool.name for tool in PROACTIVE_TOOLS}:
        return "proactive_schedules"
    if name == search_world_view.name:
        return "worldview"
    if name == search_user_info_tool.name:
        return "user_info"
    if name in {tool.name for tool in CALENDAR_TOOLS}:
        return "schedule"
    return name if name in _context_tools_by_name else "retrieval"


async def _run_context_fetcher(state: GraphState, observations: List[ToolObservation]) -> None:
    """Append each raw observation immediately, preserving partial work on timeout."""
    senders = state.get("senders") or {}
    proactive = bool((state.get("intent") or "").strip())
    history_msgs = _build_history_messages(
        state["history"],
        divider_text=None if proactive else CONTEXT_NODE_DIVIDER,
        watermark=None if proactive else state.get("responded_watermark"),
    )
    if proactive:
        # End the old transcript with the fired job, not the earlier setup request.
        history_msgs.append(SystemMessage(content=build_proactive_trigger(state["intent"])))
    now = datetime.now(SGT)
    formatted_datetime = (
        f"The current date is {now.strftime('%d %B %Y')}, "
        f"the current day of week is {now.strftime('%A')}, "
        f"the current time is {now.strftime('%H:%M')}"
    )
    participants = ", ".join(senders.values()) or "(unknown)"
    pass_number = state.get("context_passes", 0) + 1
    # Supply all earlier results and failures for the agent to evaluate.
    # This handoff is not a user turn.
    handoff = SystemMessage(content=build_context_fetcher_handoff(
        context_pass=pass_number,
        max_context_passes=settings.agent_max_context_passes,
        retrieval_request=state.get("retrieval_request", ""),
        observations=observations,
    ))
    
    request = ModelRequest(
        model=await _get_context_fetcher_llm(),
        messages=[handoff, *history_msgs],
        tools=CONTEXT_TOOLS,
        runtime=Runtime(context={"participants": participants, "datetime": formatted_datetime,
                                 "intent": state.get("intent", "")}),
    )

    async def call_model(model_request: ModelRequest) -> AIMessage:
        assert model_request.system_message is not None
        return await model_request.model.bind_tools(model_request.tools).ainvoke(
            [model_request.system_message, *model_request.messages]
        )

    ai: AIMessage = await context_fetcher_dynamic_prompt.awrap_model_call(request, call_model)
    tool_calls = getattr(ai, "tool_calls", None) or []
    print(f"[context_fetcher] LLM requested {len(tool_calls)} tool call(s):")
    for tc in tool_calls:
        print(f"[context_fetcher]   -> {tc['name']}({tc['args']})")

    for tc in tool_calls:
        name = tc["name"]
        arguments = tc["args"]
        observation: ToolObservation = {
            "pass_number": pass_number, "tool_name": name, "arguments": arguments,
            "result_category": _context_category(name),
            # An in-flight call retains this status if the pass is cancelled by
            # its deadline; completed calls replace it below.
            "status": "timeout", "raw_result": "Tool call exceeded the retrieval pass deadline.",
        }
        observations.append(observation)
        try:
            if name == search_world_view.name:
                result = await search_worldview(arguments.get("query", ""), raise_on_error=True)
                usable = bool(result and result.strip())
            elif name == search_user_info_tool.name:
                uid = _resolve_name_to_id(arguments.get("name", ""), _build_name_to_id(senders))
                if uid is None:
                    raise ValueError("Unknown or ambiguous participant name")
                facts, profile = await search_user_info(uid, arguments.get("query", ""), raise_on_error=True)
                result = {"user_id": uid, "name": senders[uid], "facts": facts, "profile": profile}
                usable = bool(facts.strip() or any(str(v).strip() for v in profile.values() if v is not None))
            else:
                tool = _context_tools_by_name.get(name)
                if tool is None:
                    raise ValueError(f"Unknown tool '{name}'")
                if _context_category(name) == "proactive_schedules":
                    result = await tool.ainvoke(arguments, config={
                        "configurable": {"current_chat_id": state.get("chat_id")},
                    })
                else:
                    result = await tool.ainvoke(arguments)
                usable = result is not None and bool(str(result).strip())
            observation.update(status="success" if usable else "empty", raw_result=result)
        except asyncio.TimeoutError as e:
            observation.update(status="timeout", raw_result=f"TimeoutError: {e}")
        except Exception as e:
            observation.update(status="error", raw_result=f"{type(e).__name__}: {e}")


async def context_fetcher_node(state: GraphState) -> Dict:
    """One deadline-bounded retrieval pass, accumulating successes and failures."""
    LLM_CALLS.labels(node="context_fetcher").inc()
    # Copy records so a timed-out task cannot mutate an earlier graph snapshot.
    observations = [dict(o) for o in state.get("tool_observations", [])]
    previous_count = len(observations)
    pass_number = state.get("context_passes", 0) + 1
    try:
        await asyncio.wait_for(_run_context_fetcher(state, observations), timeout=CONTEXT_FETCHER_TIMEOUT)
    except Exception as e:
        record_llm_error("context_fetcher", e)
        # If a tool was in-flight, its timeout observation is already present.
        # Otherwise this was the model-selection stage itself.
        if not any(o["status"] == "timeout" for o in observations[previous_count:]):
            observations.append({
                "pass_number": pass_number, "tool_name": "context_fetcher", "arguments": {},
                "result_category": "retrieval", "status": "timeout" if isinstance(e, TimeoutError) else "error",
                "raw_result": f"{type(e).__name__}: {e}",
            })
    return {
        "context_passes": pass_number,
        "tool_observations": observations,
    }


def _loop_outcome(state: GraphState, decision: str) -> str:
    if decision == "fetch_more":
        if (state.get("context_passes", 0) >= settings.agent_max_context_passes
                or state.get("remaining_steps", 3) <= 2):
            return "capped"
    return "retried" if state.get("context_passes", 0) > 1 else "first_pass"


def _route_after_responder(state: GraphState):
    if (state.get("responder_decision") == "fetch_more"
            and state.get("retrieval_request", "").strip()
            and state.get("context_passes", 0) < settings.agent_max_context_passes
            and state.get("remaining_steps", 3) > 2):
        return "context_fetcher_node"
    return END


def _response_update(state: GraphState, content: str, reason: str, decision: str = "respond",
                     request: str = "", status: str = "evaluated") -> Dict:
    # An empty handoff cannot drive a useful retrieval pass.
    if not request.strip():
        decision = "respond"
    return {
        "response_text": content, "response_reason": reason,
        "responder_decision": decision, "responder_status": status,
        "retrieval_request": request.strip() if decision == "fetch_more" else "",
        "loop_outcome": _loop_outcome(state, decision),
    }


def _salvage_reply(raw: str) -> str:
    """Extract visible content from malformed structured output without leaking internals."""
    raw = raw.strip()
    prefix = f"{BOT_NAME}:"
    if raw.startswith(prefix):
        raw = raw[len(prefix):].strip()
    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        parsed = json.loads(candidate)
    except ValueError:
        # Truncated structured JSON is not usable prose. Recover a complete
        # string content field if present; otherwise let the caller fail safely.
        if candidate.startswith(("{", "[")) or re.search(r'"(?:decision|retrieval_request|content)"\s*:', candidate):
            match = re.search(r'"content"\s*:\s*("(?:[^"\\]|\\.)*")', candidate)
            try:
                return json.loads(match.group(1)).strip() if match else ""
            except ValueError:
                return ""
    else:
        if isinstance(parsed, dict):
            content = parsed.get("content")
            return content.strip() if isinstance(content, str) else ""
        return parsed.strip() if isinstance(parsed, str) else ""
    return raw


def _parse_fallback(state: GraphState, raw: str, error: Exception) -> Dict:
    record_llm_error("responder", error)
    content = _salvage_reply(raw) or state.get("response_text") or _fallback_content(state)
    return _response_update(state, content, "Best-effort reply after structured-output parse failure.", status="parse_fallback")


def _fallback_content(state: GraphState) -> str:
    """Outreach must still initiate contact when no usable model content survives."""
    if (state.get("intent") or "").strip():
        return "Hey, how have you been?"
    return "I'm not sure yet — could you clarify what you mean?"


async def responder_node(state: GraphState) -> Dict:
    try:
        print(f"[responder] system prompt loaded (tokens={_count_tokens(RESPONDER_SYSTEM_PROMPT)})")

        personality_traits = await get_active_trait_prompts()
        print(f"[responder] personality traits fetched (tokens={_count_tokens(personality_traits)})")

        mood = state.get("mood", "default")
        communication_style = CONVERSATION_STYLE.get(mood, CONVERSATION_STYLE["default"])
        print(f"[responder] communication style fetched (tokens={_count_tokens(communication_style)})")

        context = build_responder_context(state.get("tool_observations", []))

        now = datetime.now(SGT)
        formatted_datetime = (
            f"The current date is {now.strftime('%d %B %Y')}, "
            f"the current month is {now.strftime('%B')}, "
            f"the current day of week is {now.strftime('%A')}, "
            f"the current time is {now.strftime('%H:%M')}"
        )
        proactive = bool((state.get("intent") or "").strip())
        history_msgs = _build_history_messages(
            state["history"],
            divider_text=None if proactive else RESPONDER_NODE_DIVIDER,
            watermark=None if proactive else state.get("responded_watermark"),
        )
        if proactive:
            history_msgs.append(SystemMessage(content=build_proactive_trigger(state["intent"])))
        history_tokens = sum(_count_tokens(f"{m.type}: {m.content}") for m in history_msgs)
        print(f"[responder] history fetched (count={len(history_msgs)}, tokens={history_tokens})")
        pprint(history_msgs)

        main_system_message = SystemMessage(content=build_responder_prompt(
            system_prompt=RESPONDER_SYSTEM_PROMPT,
            context=context,
            communication_style=communication_style,
            current_summary=state.get("current_summary") or "",
            personality_traits=personality_traits,
            conversation_mood=mood,
            current_datetime=formatted_datetime,
            intent=state.get("intent", ""),
        ))
        loop_system_message = SystemMessage(content=build_responder_loop_message(
            instructions=RESPONDER_LOOP_INSTRUCTIONS,
            context_passes=state.get("context_passes", 0),
            max_context_passes=settings.agent_max_context_passes,
            remaining_steps=state.get("remaining_steps"),
            context=context,
        ))
        msgs = [main_system_message, loop_system_message, *history_msgs]
        msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
        print(f"[responder] TOTAL context: {len(msgs)} messages, {msgs_tokens} tokens")

        LLM_CALLS.labels(node="responder").inc()
        try:
            result = await (await _get_responder_llm()).ainvoke(msgs)
        except OutputParserException as e:
            return _parse_fallback(state, e.llm_output or "", e)
        if isinstance(result, dict):
            raw = result.get("raw")
            content = raw.content if isinstance(raw, AIMessage) and isinstance(raw.content, str) else ""
            if result.get("parsing_error") is not None:
                return _parse_fallback(state, content, result["parsing_error"])
            try:
                result = ResponseOutput.model_validate(result.get("parsed"))
            except ValidationError as e:
                return _parse_fallback(state, content, e)
        if result is None:
            return _parse_fallback(state, "", ValueError("Responder LLM returned no parsed response"))
        print(f"[responder] LLM returned: content={result.content[:80]!r} | reason={result.reason!r}")
        return _response_update(state, result.content, result.reason, result.decision, result.retrieval_request)
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
    graph.add_conditional_edges("responder_node", _route_after_responder,
                                {"context_fetcher_node": "context_fetcher_node", END: END})
    return graph.compile()


def _build_proactive_graph():
    """Build the outreach-only graph: no checker, router, or summarizer."""
    graph: StateGraph = StateGraph(GraphState)
    graph.add_node("context_fetcher_node", context_fetcher_node)
    graph.add_node("responder_node", responder_node)
    graph.add_edge(START, "context_fetcher_node")
    graph.add_edge("context_fetcher_node", "responder_node")
    graph.add_conditional_edges("responder_node", _route_after_responder,
                                {"context_fetcher_node": "context_fetcher_node", END: END})
    return graph.compile()


_graph = _build_graph()
_proactive_graph = _build_proactive_graph()


async def _run_response_graph(graph, initial_state: GraphState, *, run_name: str) -> GraphState:
    """Apply the same pass budget, emergency recovery and metrics to both entry points."""
    result = initial_state
    try:
        async for snapshot in graph.astream(initial_state, config={
            "run_name": run_name,
            "recursion_limit": 2 * settings.agent_max_context_passes + 8,
        }, stream_mode="values"):
            result = snapshot
    except GraphRecursionError as e:
        record_llm_error("responder", e)
        result = {**result, "loop_outcome": "capped"}
        if not result.get("response_text"):
            result.update(response_text=_fallback_content(initial_state),
                          response_reason="Emergency graph recursion fallback.")
    if result.get("context_passes", 0):
        AGENT_LOOP_OUTCOMES.labels(outcome=result["loop_outcome"]).inc()
        AGENT_CONTEXT_PASSES.observe(result["context_passes"])
    return result


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
        "chat_id": chat_id,
        "intent": "",
        "current_summary": current_summary,
        "mood": current_mood,
        "senders": senders or {},
        "must_reply": must_reply,
        "is_private": is_private,
        "responded_watermark": responded_watermark,
        "should_reply": True,
        "context_passes": 0,
        "tool_observations": [],
        "retrieval_request": "",
        "responder_decision": "respond",
        "response_text": "",
        "response_reason": "",
    }

    result = await _run_response_graph(_graph, initial_state, run_name="RachelReplyGraph")

    if chat_id is not None:
        _chat_mood[chat_id] = result["mood"]
        print(f"[{chat_id}] Stored mood for next call: {result['mood']}")

    new_summary = result["current_summary"] if result["current_summary"] != current_summary else None
    return result["response_text"], result["response_reason"], new_summary, time.time() - start


async def get_proactive_response(
    history: List[Dict[str, str]],
    intent: str,
    current_summary: str | None = None,
    chat_id: int | None = None,
    senders: Dict[int, str] | None = None,
) -> Tuple[str, str, float]:
    """Generate an unconditional outreach without routing, summarizing, or mood writes."""
    if not intent.strip():
        raise ValueError("Proactive outreach requires a non-empty intent")
    start = time.time()
    current_mood = _chat_mood.get(chat_id, DEFAULT_MOOD) if chat_id is not None else DEFAULT_MOOD
    initial_state: GraphState = {
        "history": history,
        "chat_id": chat_id,
        "intent": intent,
        "current_summary": current_summary,
        "mood": current_mood,
        "senders": senders or {},
        "must_reply": True,
        "is_private": False,
        "responded_watermark": None,
        "should_reply": True,
        "context_passes": 0,
        "tool_observations": [],
        "retrieval_request": "",
        "responder_decision": "respond",
        "response_text": "",
        "response_reason": "",
    }
    result = await _run_response_graph(_proactive_graph, initial_state, run_name="RachelProactiveGraph")
    return result["response_text"], result["response_reason"], time.time() - start
