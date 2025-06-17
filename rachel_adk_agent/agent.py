import os
import time
import asyncio
from typing import Any, Dict, List, Tuple
from pydantic import BaseModel
import functools

from google.adk.agents import Agent
from google.genai import types as genai_types

from .prompts import SYSTEM_PROMPT

BOT_NAME = "Rachel"
ADK_MODEL = "gemini-1.5-flash-latest"
ADK_TEMPERATURE = 0.7
ADK_TOP_P = 0.95

class llm_response(BaseModel):
    sender: str
    content: str

def run_in_executor(f):
    @functools.wraps(f)
    async def inner(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: f(*args, **kwargs))
    return inner

@run_in_executor
async def summarise_text_tool(text: str) -> Dict[str, str]:
    """
    Summarises the given text.
    Use this tool if you need to condense a long piece of text into a shorter summary.
    Args:
        text (str): The text to be summarised.
    Returns:
        A dictionary containing the 'summary_text'.
    """
    print(f"Summarise_text_tool called with text of length: {len(text)}")
    return {"summary_text": f"Summary of: {text[:50]}..."}

# Ensure BOT_NAME is correctly referenced in the f-string style within the instruction.
instruction_string = SYSTEM_PROMPT + f"""

When responding, structure your final answer as a JSON object with two keys: 'sender' (which should be your name, "{BOT_NAME}") and 'content' (which is your textual response, potentially multi-paragraph formatted with \\n\\n)."""

root_agent = Agent(
    name=BOT_NAME + "_adk",
    model=ADK_MODEL,
    description="A friendly and helpful agent for a Telegram group chat, embodying the persona of Rachel.",
    instruction=instruction_string,
    tools=[summarise_text_tool],
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=float(ADK_TEMPERATURE),
        top_p=float(ADK_TOP_P),
    )
)

print(f"ADK Agent '{root_agent.name}' initialized with model '{root_agent.model}' and {len(root_agent.tools)} tools.")
