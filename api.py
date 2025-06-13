import os
import time
import asyncio
from typing import Any, Dict, List
#import nlpcloud
from google import genai
from google.genai import types
import functools
from dotenv import load_dotenv

from db import get_context

load_dotenv()

#client = nlpcloud.Client("chatdolphin", os.environ["NLPCLOUD_API_KEY"], gpu=True)
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# import datetime
# def get_datetime():
#     tz = datetime.timezone(datetime.timedelta(hours=8))
#     return datetime.datetime.now(tz).strftime("%d %b, %a, %H%Mh").lower()


def run_in_executor(f):
    @functools.wraps(f)
    async def inner(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: f(*args, **kwargs))

    return inner


@run_in_executor
def get_response(message, history:List[Dict[str,str]], current_summary=None):
    start = time.time()
    context:str = get_context() # context is system prompt for the bot
    if current_summary:
        print("Current summary: ", current_summary)
        context += "\n\n"
        context += current_summary

    # so now the context is the system prompt and the current summary



    # resp = client.chatbot(
    #     message,
    #     context=context,
    #     history=history,
    # )


    user_msg: Dict[str, Any] = create_gemini_content_obj(
        role="user", text=message
    )

    gemini_formatted_history = transform_history_to_gemini_format(history)

    print("System prompt: ", context)
    print(f"Message: {message}")
    print(f"History: {gemini_formatted_history}")

    response = client.models.generate_content(
    model="gemini-2.0-flash",
    #contents=[context, message, "History: ",] + history,
    config=types.GenerateContentConfig(
        system_instruction=context,
        temperature=0.2,
        topP = 0.95),
    contents=gemini_formatted_history + [user_msg]
    
    )
    end = time.time()

    # return resp["response"], end - start
    print("Response: ", response.text)
    return response.text, end - start


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
def create_gemini_content_obj(role: str, text: str) -> dict:
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
                "text": text
            }
        ]
    }


def transform_history_to_gemini_format(history: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """
    Transforms a history list into a list of dictionaries using create_gemini_content_obj.

    Args:
        history (List[Dict[str, str]]): The conversation history, where each entry is a dictionary
                                        with 'input' (user message) and 'response' (bot reply).

    Returns:
        List[Dict[str, Any]]: A list of dictionaries formatted for Gemini API.
    """
    gemini_history = []
    for entry in history:
        gemini_history.append(create_gemini_content_obj(role="user", text=entry["input"]))
        gemini_history.append(create_gemini_content_obj(role="model", text=entry["response"]))
    return gemini_history