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
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.prompts import ChatPromptTemplate
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import CONVERSATION_TONE_TEMPLATES
from app.repository import get_responder_system_prompt, get_summarizer_system_prompt

settings = get_settings()
BOT_NAME = settings.bot_name

_chat_mood: Dict[int, str] = {}
DEFAULT_MOOD = "default"

# Mood labels are defined by CONVERSATION_TONE_TEMPLATES' keys, not a static
# Literal — Field(json_schema_extra={"enum": ...}) lets the structured-output
# schema track that dict dynamically.
MOOD_LABELS = list(CONVERSATION_TONE_TEMPLATES)


class SummarizerOutput(BaseModel):
    summary: str = Field(
        ...,
        description="New 100-word summary of the conversation, or the literal string 'NIL' if the old summary is still sufficient.",
    )
    mood: str = Field(
        ...,
        description="The detected conversational mood.",
        json_schema_extra={"enum": MOOD_LABELS},
    )


class ResponseOutput(BaseModel):
    sender: str
    content: str


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

_responder_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.2,
).with_structured_output(ResponseOutput)


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
    if result.summary.strip().upper() == "NIL":
        return {"mood": result.mood}
    return {"mood": result.mood, "current_summary": result.summary}


async def responder_node(state: GraphState) -> Dict:
    mood = state.get("mood", "default")
    tone_examples = CONVERSATION_TONE_TEMPLATES.get(mood, CONVERSATION_TONE_TEMPLATES["default"])

    examples_text = "\n\n".join(
        f"User: {ex['input']}\nRachel: {ex['response']}"
        for ex in tone_examples
    )

    system_prompt_str = await get_responder_system_prompt()

    history_msgs = [
        (
            "assistant" if entry["sender"] == BOT_NAME else "human",
            f"{entry['sender']}: {entry['content']}",
        )
        for entry in state["history"]
    ]

    prompt = ChatPromptTemplate.from_messages([("system", system_prompt_str), *history_msgs])
    msgs = prompt.format_messages(
        examples_text=examples_text,
        current_summary=state.get("current_summary") or "",
    )


    print(f"Responder mood: {mood} | history length: {len(state['history'])}")

    result: ResponseOutput = await _responder_llm.ainvoke(msgs)
    print(f"Response from LLM: {result.content}")
    return {"response_text": result.content}


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


async def summarise_text(text: str) -> str:
    return "Summarised text"
