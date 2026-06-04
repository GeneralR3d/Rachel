"""LLM integration via OpenRouter (OpenAI-compatible API).

Replaces the google-genai client. The public interface (get_response, summarise_text)
is unchanged so telegram/client.py needs no edits.
"""

import json
import time
from typing import Any, Dict, List, Tuple

from openai import AsyncOpenAI

from app.config import get_settings
from app.repository import get_system_prompt

settings = get_settings()

_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=settings.openrouter_api_key,
)
BOT_NAME = settings.bot_name


async def get_response(
    current_messages: List[Dict[str, str]],
    history: List[Dict[str, str]],
    current_summary: str | None = None,
) -> Tuple[str, float]:
    """Generate a response via OpenRouter.

    Returns a ``(response_text, elapsed_seconds)`` tuple.
    """
    start = time.time()

    system_prompt: str = await get_system_prompt()
    if current_summary:
        print("Current summary: ", current_summary)
        system_prompt += "\n\n" + current_summary
    system_prompt += (
        '\n\nAlways reply with valid JSON: {"sender": "<your name>", "content": "<your reply>"}'
    )

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    for entry in history:
        role = "assistant" if entry["sender"] == BOT_NAME else "user"
        messages.append(
            {"role": role, "content": str({"sender": entry["sender"], "content": entry["content"]})}
        )

    for msg in current_messages:
        messages.append(
            {"role": "user", "content": str({"sender": msg["sender"], "content": msg["content"]})}
        )

    print(f"Messages going into model: {messages}")

    response = await _client.chat.completions.create(
        model=settings.openrouter_model,
        messages=messages,
        temperature=0.2,
        response_format={"type": "json_object"},
    )

    end = time.time()

    response_text = ""
    try:
        data = json.loads(response.choices[0].message.content)
        response_text = data["content"]
    except Exception as e:
        print("Error parsing response: ", e)
        response_text = "Sorry, I encountered an error while processing your request."

    print("Response from LLM: ", response_text)
    return response_text, end - start


async def summarise_text(text: str) -> str:
    # Stubbed — same as before.
    return "Summarised text"
