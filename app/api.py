import os
import time
import asyncio
from typing import Any, Dict, List, Tuple
#import nlpcloud
from google import genai
from pydantic import BaseModel
from google.genai import types
import functools
from dotenv import load_dotenv

from app.db import get_system_prompt

load_dotenv()

#client = nlpcloud.Client("chatdolphin", os.environ["NLPCLOUD_API_KEY"], gpu=True)
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
BOT_NAME = os.environ.get("BOT_NAME", "Rachel")  # mostly for summary use


# import datetime
# def get_datetime():
#     tz = datetime.timezone(datetime.timedelta(hours=8))
#     return datetime.datetime.now(tz).strftime("%d %b, %a, %H%Mh").lower()

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
def get_response(current_messages: List[Dict[str, str]], history: List[Dict[str, str]], current_summary=None) -> Tuple[str, float]:
    """
    Generates a response using the Gemini API.

    Args:
        current_messages (List[Dict[str, str]]): List of current messages with sender, content, and message_id.
        history (List[Dict[str, str]]): Conversation history.
        current_summary (str, optional): Current summary of the conversation.

    Returns:
        Tuple[str, float]: The response text and the time taken to generate it.
    """
    start = time.time()
    system_prompt: str = get_system_prompt()  # context is system prompt for the bot
    if current_summary:
        print("Current summary: ", current_summary)
        system_prompt += "\n\n" + current_summary

    # Transform the current messages into Gemini format
    gemini_formatted_current_messages = [
        create_gemini_content_obj(role="user", text=msg["content"], sender=msg["sender"]) for msg in current_messages
    ]

    gemini_formatted_history = transform_history_to_gemini_format(history, BOT_NAME)

    print(f"Current Messages: {gemini_formatted_current_messages}")
    print(f"History going into model: {gemini_formatted_history}")
    print()

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
            topP=0.95,
            responseMimeType="application/json",
            responseSchema=llm_response
        ),
        contents=gemini_formatted_history + gemini_formatted_current_messages
    )
    end = time.time()
    response_text: str = ""
    try:
        response_text = response.parsed.content
    except Exception as e:
        print("Error parsing response: ", e)
        response_text = "Sorry, I encountered an error while processing your request."
    # return resp["response"], end - start
    print("Response from LLM: ", response_text)
    return response_text, end - start


@run_in_executor
def summarise_text(text: str):
    # res = client.summarization(text, size="large")

    # return res["summary_text"]
    # response = client.models.generate_content(
    # model="gemini-2.0-flash",
    # contents=["Summarise the following text: ", text],
    # )
    # return response.text
    return "Summarised text"

# helper
def create_gemini_content_obj(role: str, text: str, sender: str) -> dict:
    """
    Creates a dictionary with the specified role and text in the required format.

    Args:
        role (str): The role of the message (e.g., "user", "system").
        text (str): The text content of the message.

    Returns:
        dict: A dictionary in the specified format.
    """
    return {
        "role": role,
        "parts": [
            {
                "text": str({"sender": sender, "content": text})
            }
        ]
    }


def transform_history_to_gemini_format(history: List[Dict[str, str]],BOT_NAME:str) -> List[Dict[str, Any]]:
    """
    Transforms a history list into a list of dictionaries using create_gemini_content_obj.
    If the sender is BOT_NAME, it uses "model" as the role; otherwise, it uses "user".

    Args:
        history (List[Dict[str, str]]): The conversation history, where each entry is a dictionary
                                        with 'input' (user message) and 'response' (bot reply).

    Returns:
        List[Dict[str, Any]]: A list of dictionaries formatted for Gemini API.
    """
    return [
        create_gemini_content_obj(
            role="model" if entry["sender"] == BOT_NAME else "user",
            text=entry["content"],
            sender=entry["sender"]
        ) for entry in history
    ]