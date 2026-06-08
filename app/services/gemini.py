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

from langchain_core.prompts import ChatPromptTemplate
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import CONVERSATION_TONE_TEMPLATES
from app.repository import (
    get_active_trait_prompts,
    get_current_activity,
    get_day_summary,
    get_responder_system_prompt,
    get_summarizer_system_prompt,
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

# Mood labels are defined by CONVERSATION_TONE_TEMPLATES' keys, not a static
# Literal — Field(json_schema_extra={"enum": ...}) lets the structured-output
# schema track that dict dynamically.
MOOD_LABELS = list(CONVERSATION_TONE_TEMPLATES)


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


class GraphState(TypedDict):
    history: List[Dict[str, Any]]
    current_summary: str | None
    mood: str
    response_text: str


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
).with_structured_output(ResponseOutput, method="json_mode")


async def summarizer_node(state: GraphState) -> Dict:
    history_msgs = [
        (
            "assistant" if entry["sender"] == BOT_NAME else "human",
            f"{entry['sender']}: {entry['content']}",
        )
        for entry in state["history"]
    ]

    system_prompt_str = await get_summarizer_system_prompt()

    prompt = ChatPromptTemplate.from_messages([("system", system_prompt_str), *history_msgs])
    msgs = prompt.format_messages(
        mood_list = MOOD_LABELS,
        old_summary=state.get("current_summary") or "")

    result: SummarizerOutput = await _summarizer_llm.ainvoke(msgs)
    print(f"Detected mood: {result.mood} | Summary: {result.summary}")
    if result.summary == "NIL":
        return {"mood": result.mood}
    return {"mood": result.mood, "current_summary": result.summary}


async def responder_node(state: GraphState) -> Dict:
    print("[responder] node entered")
    try:
        mood = state.get("mood", "default")
        tone_examples = CONVERSATION_TONE_TEMPLATES.get(mood, CONVERSATION_TONE_TEMPLATES["default"])

        examples_text = "\n\n".join(
            f"User: {ex['input']}\nRachel: {ex['response']}"
            for ex in tone_examples
        )
        print(f"[responder] mood={mood} | examples built")

        system_prompt_str = await get_responder_system_prompt()
        print(f"[responder] system prompt fetched (len={len(system_prompt_str)})")

        personality_traits = await get_active_trait_prompts()
        print(f"[responder] personality traits fetched (len={len(personality_traits)})")

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

        history_msgs = [
            (
                "assistant" if entry["sender"] == BOT_NAME else "human",
                f"{entry['sender']}: {entry['content']}",
            )
            for entry in state["history"]
        ]
        print(f"[responder] history_msgs built (count={len(history_msgs)})")

        prompt = ChatPromptTemplate.from_messages([("system", system_prompt_str), *history_msgs])
        print(f"[responder] template created, input_variables={prompt.input_variables}")

        msgs = prompt.format_messages(
            examples_text=examples_text,
            current_summary=state.get("current_summary") or "",
            personality_traits=personality_traits,
            conversation_mood=mood,
            datetime=formatted_datetime,
            current_activity=current_activity or "Nothing scheduled right now",
            day_summary=day_summary,
        )
        print(f"[responder] messages formatted (count={len(msgs)})")

        print("[responder] calling LLM now...")
        result: ResponseOutput = await _responder_llm.ainvoke(msgs)
        print(f"[responder] LLM returned: content={result.content[:80]!r}")
        return {"response_text": result.content}
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
) -> Tuple[str, str | None, float]:
    """Run the LangGraph pipeline and return ``(response_text, elapsed_seconds)``.

    summarizer_node and responder_node run in parallel.  The mood detected this
    call is stored in _chat_mood and injected on the *next* call.
    """
    start = time.time()



    current_mood = _chat_mood.get(chat_id, DEFAULT_MOOD) if chat_id is not None else DEFAULT_MOOD

    initial_state: GraphState = {
        "history": history,
        "current_summary": current_summary,
        "mood": current_mood,
        "response_text": "",
    }

    result = await _graph.ainvoke(initial_state)

    if chat_id is not None:
        _chat_mood[chat_id] = result["mood"]
        print(f"[{chat_id}] Stored mood for next call: {result['mood']}")

    new_summary = result["current_summary"] if result["current_summary"] != current_summary else None
    return result["response_text"], new_summary, time.time() - start
