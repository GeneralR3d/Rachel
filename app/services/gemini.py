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
from typing import Any, Dict, List, Tuple

from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.config import get_settings
from app.prompts import CONVERSATION_TONE_TEMPLATES, SUMMARIZER_SYSTEM_PROMPT
from app.repository import get_responder_system_prompt

settings = get_settings()
BOT_NAME = settings.bot_name

_chat_mood: Dict[int, str] = {}
DEFAULT_MOOD = "default"

# Mood labels are defined by CONVERSATION_TONE_TEMPLATES' keys, not a static
# Literal — Field(json_schema_extra={"enum": ...}) lets the structured-output
# schema track that dict dynamically.
MOOD_LABELS = list(CONVERSATION_TONE_TEMPLATES)


class MoodOutput(BaseModel):
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
    responser_system_prompt: str
    mood: str
    response_text: str


_summarizer_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.0,
).with_structured_output(MoodOutput)

_responder_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.openrouter_api_key,
    temperature=0.2,
).with_structured_output(ResponseOutput)


async def summarizer_node(state: GraphState) -> Dict:
    msgs: List[tuple] = [("system", SUMMARIZER_SYSTEM_PROMPT)]

    if state.get("current_summary"):
        msgs.append(("human", f"Previous context: {state['current_summary']}"))

    # Use history since current_messages is always empty in practice
    for entry in state["history"]:
        msgs.append(("human", f"{entry['sender']}: {entry['content']}"))

    result: MoodOutput = await _summarizer_llm.ainvoke(msgs)
    print(f"Detected mood: {result.mood}")
    return {"mood": result.mood}


async def responder_node(state: GraphState) -> Dict:
    mood = state.get("mood", "default")
    tone_examples = CONVERSATION_TONE_TEMPLATES.get(mood, CONVERSATION_TONE_TEMPLATES["default"])

    examples_text = "\n\n".join(
        f"User: {ex['input']}\nRachel: {ex['response']}"
        for ex in tone_examples
    )

    responser_system_prompt = (await get_responder_system_prompt()).replace(
        "<Communication Examples>\n</Communication Examples>",
        f"<Communication Examples>\n{examples_text}\n</Communication Examples>",
    )

    if state.get("current_summary"):
        responser_system_prompt += "\n\n" + state["current_summary"]

    msgs: List[tuple] = [("system", responser_system_prompt)]

    for entry in state["history"]:
        role = "assistant" if entry["sender"] == BOT_NAME else "human"
        msgs.append((role, str({"sender": entry["sender"], "content": entry["content"]})))


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
) -> Tuple[str, float]:
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

    return result["response_text"], time.time() - start


async def summarise_text(text: str) -> str:
    return "Summarised text"
