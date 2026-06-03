"""Google Gemini integration — async port of the original Reference/app/api.py.

The blocking ``generate_content`` SDK call is offloaded to a worker thread via
``asyncio.to_thread`` so it never blocks the event loop (replacing the original
``run_in_executor`` decorator).
"""

import asyncio
import time
from typing import Any, Dict, List, Tuple

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.config import get_settings
from app.repository import get_system_prompt

settings = get_settings()

client = genai.Client(api_key=settings.gemini_api_key)
BOT_NAME = settings.bot_name


class llm_response(BaseModel):
    sender: str
    content: str


async def get_response(
    current_messages: List[Dict[str, str]],
    history: List[Dict[str, str]],
    current_summary: str | None = None,
) -> Tuple[str, float]:
    """Generate a response using the Gemini API.

    Returns a ``(response_text, elapsed_seconds)`` tuple.
    """
    start = time.time()
    system_prompt: str = await get_system_prompt()
    if current_summary:
        print("Current summary: ", current_summary)
        system_prompt += "\n\n" + current_summary

    gemini_formatted_current_messages = [
        create_gemini_content_obj(role="user", text=msg["content"], sender=msg["sender"])
        for msg in current_messages
    ]
    gemini_formatted_history = transform_history_to_gemini_format(history, BOT_NAME)

    print(f"Current Messages: {gemini_formatted_current_messages}")
    print(f"History going into model: {gemini_formatted_history}")
    print()

    def _call() -> Any:
        return client.models.generate_content(
            model="gemini-2.0-flash",
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,
                topP=0.95,
                responseMimeType="application/json",
                responseSchema=llm_response,
            ),
            contents=gemini_formatted_history + gemini_formatted_current_messages,
        )

    response = await asyncio.to_thread(_call)
    end = time.time()

    response_text: str = ""
    try:
        response_text = response.parsed.content
    except Exception as e:
        print("Error parsing response: ", e)
        response_text = "Sorry, I encountered an error while processing your request."
    print("Response from LLM: ", response_text)
    return response_text, end - start


async def summarise_text(text: str) -> str:
    # Stubbed in the reference implementation; kept identical here.
    return "Summarised text"


# helpers ------------------------------------------------------------------


def create_gemini_content_obj(role: str, text: str, sender: str) -> dict:
    return {
        "role": role,
        "parts": [{"text": str({"sender": sender, "content": text})}],
    }


def transform_history_to_gemini_format(
    history: List[Dict[str, str]], BOT_NAME: str
) -> List[Dict[str, Any]]:
    """Use 'model' role for the bot's own messages, 'user' otherwise."""
    return [
        create_gemini_content_obj(
            role="model" if entry["sender"] == BOT_NAME else "user",
            text=entry["content"],
            sender=entry["sender"],
        )
        for entry in history
    ]
