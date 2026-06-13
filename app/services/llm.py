"""LLM integration via LangGraph + LangChain OpenRouter.

Both nodes fan out from START and run in parallel:

  START → summarizer_node  ↘
        → responder_node   → END

summarizer_node: detects conversation mood; result is stored in _chat_mood
                 and used by the NEXT call's responder_node.
responder_node:  generates Rachel's reply using the mood detected in the
                 previous call (defaults to "default" on first contact).
"""

import time
from datetime import datetime
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
from app.prompts import CONVERSATION_STYLE
from app.services.worldview import read_worldview
from app.repository import (
    get_active_trait_prompts,
    get_current_activity,
    get_day_summary,
    get_responder_system_prompt,
    get_summarizer_system_prompt,
    get_user_facts_batch,
)

settings = get_settings()
BOT_NAME = settings.bot_name

_chat_mood: Dict[int, str] = {}
DEFAULT_MOOD = "default"

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

# Mood labels are defined by CONVERSATION_STYLE's keys, not a static
# Literal — Field(json_schema_extra={"enum": ...}) lets the structured-output
# schema track that dict dynamically.
MOOD_LABELS = list(CONVERSATION_STYLE)


class SummarizerOutput(BaseModel):
    summary: str = Field(
        ...,
        description='New 100-word summary of the conversation, or the exact string "NIL" if the old summary is still sufficient.',
    )
    mood: str = Field(
        ...,
        description="The detected conversational mood.",
        json_schema_extra={"enum": MOOD_LABELS},
    )


class ResponseOutput(BaseModel):
    content: str = Field(
        ...,
        description="Rachel's reply as plain text in her natural Singlish voice. Use \\n\\n to separate message bursts. Do NOT include her name or any prefix.",
    )
    reason: str = Field(
        ...,
        description="A single sentence explaining why this reply was given, naming which part of the personality traits or system prompt instructions drove it. For traceability and debugging.",
    )


class GraphState(TypedDict):
    history: List[Dict[str, Any]]
    current_summary: str | None
    mood: str
    sender_user_ids: List[int]
    response_text: str
    response_reason: str


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

        world_view = read_worldview() or "Nothing learned yet."
        print(f"[responder] world view fetched (tokens={_count_tokens(communication_style)})")
        pprint(world_view)


        # Pull stored facts/preferences for all participants (cached per user).
        sender_user_ids = state.get("sender_user_ids") or []
        print(f"All users is {sender_user_ids}")
        facts_by_user = await _get_user_facts_cached(sender_user_ids)
        facts_blocks = [
            f"User {uid}:\n{facts.strip()}"
            for uid in sender_user_ids
            if (facts := facts_by_user.get(uid, "").strip())
        ]
        user_facts = "\n\n".join(facts_blocks) or "Nothing learned yet."
        print(f"[responder] user facts fetched for {len(sender_user_ids)} sender(s) (tokens={_count_tokens(user_facts)})")
        pprint(user_facts)

        now = datetime.now()
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
        )
        msgs = [*system_msgs, *history_msgs]
        msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
        print(f"[responder] context: {len(msgs)} messages, {msgs_tokens} tokens")

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
    graph.add_node("summarizer_node", summarizer_node)
    graph.add_node("responder_node", responder_node)
    graph.add_edge(START, "summarizer_node")
    graph.add_edge(START, "responder_node")
    graph.add_edge("summarizer_node", END)
    graph.add_edge("responder_node", END)
    return graph.compile()


_graph = _build_graph()


async def get_response(
    history: List[Dict[str, str]],
    current_summary: str | None = None,
    chat_id: int | None = None,
    sender_user_ids: List[int] | None = None,
) -> Tuple[str, str, str | None, float]:
    """Run the LangGraph pipeline and return ``(response_text, reason, new_summary, elapsed_seconds)``.

    summarizer_node and responder_node run in parallel.  The mood detected this
    call is stored in _chat_mood and injected on the *next* call.
    """
    start = time.time()



    current_mood = _chat_mood.get(chat_id, DEFAULT_MOOD) if chat_id is not None else DEFAULT_MOOD

    initial_state: GraphState = {
        "history": history,
        "current_summary": current_summary,
        "mood": current_mood,
        "sender_user_ids": sender_user_ids or [],
        "response_text": "",
        "response_reason": "",
    }

    result = await _graph.ainvoke(initial_state)

    if chat_id is not None:
        _chat_mood[chat_id] = result["mood"]
        print(f"[{chat_id}] Stored mood for next call: {result['mood']}")

    new_summary = result["current_summary"] if result["current_summary"] != current_summary else None
    return result["response_text"], result["response_reason"], new_summary, time.time() - start
