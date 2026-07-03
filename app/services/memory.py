"""Entry point + shared helpers for Rachel's post-conversation memory upkeep.

``update_memories`` wraps the two memory pipelines — the general world view
(app/services/worldview.py) and the per-user facts/profile store
(app/services/userfacts.py) — behind a single call. It divides the finished
conversation at the memory watermark exactly ONCE here (via
``build_extraction_history``) and feeds the resulting pre-divided extraction
history into both pipelines, so the (identical) division work isn't repeated in
each pipeline. Both pipelines still run concurrently and neither ever raises.

The watermark rationale: both pipelines run once per finished conversation over
the same snapshot, and both re-see messages that were already processed in an
earlier finalize (the buffer is re-seeded with recent history on first contact).
To stop the fact extractors re-extracting from those, each chat carries a
high-water mark (``chat_state.last_processed_message_id``): messages at or below
it are prior, already-processed context, and only newer ones are extracted from.
``build_extraction_history`` renders the conversation as LLM turns and inserts a
divider before the first not-yet-processed message, mirroring the divider the
responder uses (see app/services/llm.py).

Called as a detached task from app/telegram/client.py::finalize_conversation.
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage

from app.config import get_settings
from app.services.userfacts import update_user_facts
from app.services.worldview import update_worldview

BOT_NAME = get_settings().bot_name

# Divider inserted between already-processed context and the new messages the
# extractors should pull facts from. Shared by both pipelines' fact extractors.
EXTRACTOR_DIVIDER = (
    "[EVERYTHING ABOVE IS EARLIER CONVERSATION ALREADY PROCESSED IN A PREVIOUS "
    "ROUND — CONTEXT ONLY. EXTRACT NEW FACTS ONLY FROM THE MESSAGE(S) BELOW; DO "
    "NOT RE-EXTRACT ANYTHING ABOVE.]"
)


def _new_index(conversation: List[Dict[str, Any]], last_processed_id: int) -> int:
    """Index of the first message newer than the watermark, or len() if none."""
    for i, entry in enumerate(conversation):
        if int(entry.get("telegram_message_id") or 0) > last_processed_id:
            return i
    return len(conversation)


def build_extraction_history(
    conversation: List[Dict[str, Any]],
    last_processed_id: int = 0,
    divider_text: Optional[str] = EXTRACTOR_DIVIDER,
) -> Tuple[List[Any], bool]:
    """Render ``conversation`` as AIMessage/HumanMessage turns for a fact extractor.

    Messages with ``telegram_message_id <= last_processed_id`` are already-processed
    context; a divider (``divider_text``) is inserted before the first newer message
    so the extractor only pulls facts from what follows. Messages are concrete
    Message objects (not templated tuples) so literal '{'/'}' in content passes
    through verbatim.

    Returns ``(history_msgs, has_new)`` — ``has_new`` is False when every message is
    at or below the watermark (nothing to extract), letting the caller short-circuit.
    When there is no prior context (all messages are new, e.g. first ever run) no
    divider is inserted.
    """
    idx = _new_index(conversation, last_processed_id)
    has_new = idx < len(conversation)
    msgs: List[Any] = []
    for i, entry in enumerate(conversation):
        if divider_text is not None and i == idx and 0 < idx < len(conversation):
            msgs.append(HumanMessage(content=divider_text))
        msgs.append(
            AIMessage(content=f"{entry['sender']}: {entry['content']}")
            if entry["sender"] == BOT_NAME
            else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
        )
    return msgs, has_new


async def update_memories(
    conversation: List[Dict[str, Any]],
    summary: str = "",
    chat_id: int | None = None,
    last_processed_id: int = 0,
) -> None:
    """Run both memory pipelines on a finished conversation.

    ``last_processed_id`` is the chat's memory watermark: the conversation is
    divided here once into already-processed context (at or below it) and new
    messages (above it), and only the new part is extracted from. The world-view
    pipeline needs only the divided extraction history; the user-facts pipeline
    also needs the raw ``conversation`` (for name→id resolution and its profile
    branch). Never raises — both pipelines swallow their own errors.
    """
    if not conversation:
        return

    extraction_msgs, has_new = build_extraction_history(conversation, last_processed_id)
    await asyncio.gather(
        update_worldview(extraction_msgs, has_new, chat_id),
        update_user_facts(conversation, extraction_msgs, has_new, summary, chat_id),
    )
