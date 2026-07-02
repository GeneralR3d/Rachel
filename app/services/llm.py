"""LLM integration via LangGraph + LangChain OpenRouter.

The nodes run sequentially, gated by a checker + router up front:

  START → checker_node → (must_reply?) → summarizer_node → responder_node → END
                ↓ no                          ↑
              router_node → (reply needed?) ──┘
                ↓ no
               END

checker_node:    cheap, no-LLM gate. If MUST_REPLY is set (1-on-1 chat or Rachel
                 was tagged), skip the router entirely and go straight to the
                 summarizer/responder. Otherwise defer to the router.
router_node:     decides whether a reply is even warranted. If not, the graph
                 short-circuits straight to END (no summary, no response).
summarizer_node: detects conversation mood; result is stored in _chat_mood
                 and used by the NEXT call's responder_node. Runs before the
                 responder so its summary update is visible to the responder.
responder_node:  generates Rachel's reply using the mood detected in the
                 previous call (defaults to "default" on first contact).
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))
from typing import Any, Dict, List, Tuple
from pprint import pprint

import tiktoken

from app.services import worldview

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
    CONVERSATION_STYLE,
    ROUTER_PM_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    USER_PROFILE_ATTRIBUTE_GUIDE,
    USER_PROFILE_FIELDS,
)
from app.services.worldview import read_worldview
from app.repository import (
    get_active_trait_prompts,
    get_current_activity,
    get_day_summary,
    get_responder_system_prompt,
    get_summarizer_system_prompt,
    get_user_facts_batch,
    get_user_profiles_batch,
)

settings = get_settings()
BOT_NAME = settings.bot_name

# The router only needs the tail of the conversation to decide whether a reply
# is warranted, so cap how many of the most recent buffer messages it sees.
ROUTER_CONTEXT_MSGS = 15

_chat_mood: Dict[int, str] = {}
DEFAULT_MOOD = "default"


def get_chat_mood(chat_id: int) -> str:
    """Return the last-detected mood for a chat (DEFAULT_MOOD if none)."""
    return _chat_mood.get(chat_id, DEFAULT_MOOD)


def set_chat_mood(chat_id: int, mood: str) -> None:
    """Seed the in-memory mood for a chat (e.g. restoring from DB on first load)."""
    _chat_mood[chat_id] = mood

# Schedule lookups only change once per hour (current_activity) / once per day
# (day_summary), so cache the last result keyed on what it depends on instead
# of hitting the DB on every message.
_current_activity_cache: Tuple[int, int, Any] | None = None  # (day_of_week, hour, activity)
_day_summary_cache: Tuple[int, List[Dict[str, Any]]] | None = None  # (day_of_week, summary)

# Per-user facts change only when the userfacts pipeline runs (once per finished
# conversation), so cache them per user_id with a short TTL instead of hitting
# the DB on every message. Worst case staleness is USER_FACTS_CACHE_TTL seconds.
USER_FACTS_CACHE_TTL = 5 * 60    # 5 min
_user_facts_cache: Dict[int, Tuple[str, float]] = {}  # user_id -> (facts, fetched_at)


def update_user_facts_cache(user_id: int, facts: str) -> None:
    """Write-through the cache after the userfacts pipeline persists a profile.

    Called from userfacts.consolidation_node so the responder sees a freshly
    consolidated profile immediately instead of waiting out USER_FACTS_CACHE_TTL
    (or serving the stale pre-consolidation value).
    """
    _user_facts_cache[user_id] = (facts, time.monotonic())


async def _get_user_facts_cached(user_ids: List[int]) -> Dict[int, str]:
    """Return {user_id: facts} for the given users, serving fresh cache hits and
    batching only the stale/missing ones into a single DB call."""
    now = time.monotonic()
    result: Dict[int, str] = {}
    stale: List[int] = []
    for uid in user_ids:
        cached = _user_facts_cache.get(uid)
        if cached is not None and (now - cached[1]) < USER_FACTS_CACHE_TTL:
            result[uid] = cached[0]
        else:
            stale.append(uid)

    if stale:
        fetched = await get_user_facts_batch(stale)
        for uid in stale:
            # Cache "" for users with no facts too, so we don't re-query them.
            facts = fetched.get(uid, "")
            _user_facts_cache[uid] = (facts, now)
            result[uid] = facts
    return result


# Structured profiles are kept in their own cache (parallel to the facts cache
# above) so the two write-throughs never have to preserve each other's value.
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
    should_reply: bool
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


def checker_node(state: GraphState) -> Dict:
    """Cheap, no-LLM gate in front of the router. Does nothing on its own; the
    routing decision lives in _route_after_checker, which reads must_reply."""
    return {}


def _route_after_checker(state: GraphState) -> str:
    """Conditional edge: if the caller forced a reply (1-on-1 chat or Rachel was
    tagged), skip the router and go straight to the summarizer. Otherwise let the
    router decide whether a reply is warranted."""
    return "summarizer_node" if state.get("must_reply", False) else "router_node"


async def router_node(state: GraphState) -> Dict:
    """Decide whether Rachel should reply at all. On failure, default to replying
    (fail-open) so a flaky router never silences her."""
    # Only show the router the most recent messages — it just needs the latest
    # context to judge whether a reply is warranted, not the whole buffer.
    recent_history = state["history"][-ROUTER_CONTEXT_MSGS:]
    history_msgs = [
        AIMessage(content=f"{entry['sender']}: {entry['content']}")
        if entry["sender"] == BOT_NAME
        else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
        for entry in recent_history
    ]

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

    try:
        result = await _router_llm.ainvoke(msgs)
    except Exception as e:
        print(f"[router] LLM error (defaulting to reply): {type(e).__name__}: {e}")
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


def _route_after_router(state: GraphState) -> str:
    """Conditional edge: run the summarizer/responder only if a reply is wanted."""
    return "summarizer_node" if state.get("should_reply", True) else END


async def summarizer_node(state: GraphState) -> Dict:
    # History is built as concrete Message objects (not templated tuples) so any
    # literal '{' or '}' in user content is passed through verbatim rather than
    # parsed as an f-string placeholder, which would crash from_messages.
    history_msgs = [
        AIMessage(content=f"{entry['sender']}: {entry['content']}")
        if entry["sender"] == BOT_NAME
        else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
        for entry in state["history"]
    ]

    system_prompt_str = await get_summarizer_system_prompt()

    system_msgs = ChatPromptTemplate.from_messages(
        [("system", system_prompt_str)]
    ).format_messages(
        mood_list=MOOD_LABELS,
        old_summary=state.get("current_summary") or "")
    msgs = [*system_msgs, *history_msgs]

    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"[summarizer] context: {len(msgs)} messages, {msgs_tokens} tokens")

    try:
        result: SummarizerOutput = await _summarizer_llm.ainvoke(msgs)
    except Exception as e:
        print(f"[summarizer] LLM error (keeping current mood/summary): {type(e).__name__}: {e}")
        return {}
    print(f"Detected mood: {result.mood} | Summary: {result.summary}")
    if result.summary == "NIL":
        return {"mood": result.mood}
    return {"mood": result.mood, "current_summary": result.summary}


async def responder_node(state: GraphState) -> Dict:
    try:


        system_prompt_str = await get_responder_system_prompt()
        print(f"[responder] system prompt fetched (tokens={_count_tokens(system_prompt_str)})")
    

        personality_traits = await get_active_trait_prompts()
        print(f"[responder] personality traits fetched (tokens={_count_tokens(personality_traits)})")

        mood = state.get("mood", "default")
        communication_style = CONVERSATION_STYLE.get(mood, CONVERSATION_STYLE["default"])
        print(f"[responder] communication style fetched (tokens={_count_tokens(communication_style)})")

        # Query Graphiti with the latest human message in the buffer as the
        # search text (falls back to the whole buffer's last entry if none is
        # from a user).
        worldview_query = next(
            (
                entry["content"]
                for entry in reversed(state["history"])
                if entry["sender"] != BOT_NAME
            ),
            "",
        )
        world_view = await read_worldview(worldview_query) or "Nothing learned yet."
        print(f"[responder] world view fetched (tokens={_count_tokens(world_view)})")


        # Pull stored facts/preferences AND structured profile for all
        # participants (each cached per user, fetched in parallel).
        senders = state.get("senders") or {}
        sender_user_ids = list(senders)
        print(f"All users is {senders}")
        facts_by_user, profiles_by_user = await asyncio.gather(
            _get_user_facts_cached(sender_user_ids),
            _get_user_profiles_cached(sender_user_ids),
        )

        def _label(uid: int) -> str:
            """Human-readable header for a participant: name + id when known."""
            name = (senders.get(uid) or "").strip()
            return f"{name} (id {uid})" if name else f"User {uid}"

        # Free-form facts → {user_facts}
        facts_blocks = [
            f"{_label(uid)}:\n{facts.strip()}"
            for uid in sender_user_ids
            if (facts := facts_by_user.get(uid, "").strip())
        ]
        user_facts = "\n\n".join(facts_blocks) or "Nothing learned yet."
        # Structured profiles → {user_profiles}; show every slot (NIL for empties)
        # so the responder knows which attributes are still gaps to subtly probe.
        profile_blocks = [
            f"{_label(uid)}:\n{_render_profile(profiles_by_user.get(uid, {}), show_unknown=True)}"
            for uid in sender_user_ids
        ]
        user_profiles = "\n\n".join(profile_blocks) or "No one identified yet."
        print(
            f"[responder] facts+profile fetched for {len(sender_user_ids)} sender(s) "
            f"(facts_tokens={_count_tokens(user_facts)}, profile_tokens={_count_tokens(user_profiles)})"
        )

        now = datetime.now(SGT)
        formatted_datetime = (
            f"The current date is {now.strftime('%d %B %Y')}, "
            f"the current month is {now.strftime('%B')}, "
            f"the current day of week is {now.strftime('%A')}, "
            f"the current time is {now.strftime('%H:%M')}"
        )
        day_of_week = now.weekday()

        global _current_activity_cache, _day_summary_cache

        if _current_activity_cache is not None and _current_activity_cache[:2] == (day_of_week, now.hour):
            current_activity = _current_activity_cache[2]
        else:
            current_activity = await get_current_activity(day_of_week, now.hour)
            _current_activity_cache = (day_of_week, now.hour, current_activity)

        if _day_summary_cache is not None and _day_summary_cache[0] == day_of_week:
            day_summary = _day_summary_cache[1]
        else:
            day_summary = await get_day_summary(day_of_week)
            _day_summary_cache = (day_of_week, day_summary)

        print(f"[responder] schedule fetched (current_activity={current_activity}, current datetime = {formatted_datetime}")

        # History is built as concrete Message objects (not templated tuples) so
        # any literal '{' or '}' in user content is passed through verbatim rather
        # than parsed as an f-string placeholder, which would crash from_messages.
        history_msgs = [
            AIMessage(content=f"{entry['sender']}: {entry['content']}")
            if entry["sender"] == BOT_NAME
            else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
            for entry in state["history"]
        ]
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
            current_activity=current_activity or "Nothing scheduled right now",
            day_summary=day_summary,
            world_view=world_view,
            user_facts=user_facts,
            user_profiles=user_profiles,
            profile_attributes=USER_PROFILE_ATTRIBUTE_GUIDE,
        )
        msgs = [*system_msgs, *history_msgs]
        msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
        print(f"[responder] TOTAL context: {len(msgs)} messages, {msgs_tokens} tokens")

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
            print(f"[responder] json parse failed; salvaging raw text ({len(raw)} chars)")
            return {
                "response_text": raw,
                "response_reason": "Salvaged from non-JSON model output (json_mode parse failure).",
            }
        print(f"[responder] LLM returned: content={result.content[:80]!r} | reason={result.reason!r}")
        return {"response_text": result.content, "response_reason": result.reason}
    except BaseException as e:
        import traceback
        print(f"[responder] EXCEPTION {type(e).__name__}: {e}")
        traceback.print_exc()
        raise


def _build_graph():
    graph: StateGraph = StateGraph(GraphState)
    graph.add_node("checker_node", checker_node)
    graph.add_node("router_node", router_node)
    graph.add_node("summarizer_node", summarizer_node)
    graph.add_node("responder_node", responder_node)
    graph.add_edge(START, "checker_node")
    # Checker gates the router: a forced reply (must_reply) skips straight to the
    # summarizer; otherwise the router decides whether a reply is warranted.
    graph.add_conditional_edges(
        "checker_node",
        _route_after_checker,
        {"summarizer_node": "summarizer_node", "router_node": "router_node"},
    )
    # Router gates everything: if no reply is warranted, jump straight to END.
    graph.add_conditional_edges(
        "router_node",
        _route_after_router,
        {"summarizer_node": "summarizer_node", END: END},
    )
    # Summarizer runs before the responder (sequential) so its mood/summary
    # update is visible to the responder in the same call.
    graph.add_edge("summarizer_node", "responder_node")
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
        "should_reply": True,
        "response_text": "",
        "response_reason": "",
    }

    result = await _graph.ainvoke(initial_state)

    if chat_id is not None:
        _chat_mood[chat_id] = result["mood"]
        print(f"[{chat_id}] Stored mood for next call: {result['mood']}")

    new_summary = result["current_summary"] if result["current_summary"] != current_summary else None
    return result["response_text"], result["response_reason"], new_summary, time.time() - start
