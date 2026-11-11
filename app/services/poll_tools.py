"""Chat-scoped poll delivery with a receipt shared across whole-graph retries."""

import asyncio
from dataclasses import dataclass, field
from secrets import randbits
from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, field_validator
from telethon.errors import RPCError
from telethon.tl.types import InputMediaPoll, Poll, PollAnswer, TextWithEntities

POLL_SEND_TIMEOUT = 10
POLL_REASON = "Sent a poll requested by the context fetcher."


class SendPollArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str
    options: list[str]

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 300:
            raise ValueError("Poll question must contain 1–300 characters")
        return value

    @field_validator("options")
    @classmethod
    def validate_options(cls, values: list[str]) -> list[str]:
        values = [value.strip() for value in values]
        if not 2 <= len(values) <= 10 or any(not 1 <= len(value) <= 100 for value in values):
            raise ValueError("Poll requires 2–10 nonblank options of at most 100 characters each")
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("Poll options must be distinct")
        return values


@dataclass
class PollReceipt:
    message_id: int
    content: str
    recorded: bool = False


@dataclass
class PollDeliveryRuntime:
    """Application-owned turn state; never exposed as model arguments.

    Caller holds the reply lock. Once a send starts, any unconfirmed outcome
    blocks another send of that same poll for this turn, including a whole-graph
    retry. Distinct polls are independent side effects.
    """

    chat_id: int
    sender_id: int
    client: Any
    record_message: Callable[..., Awaitable[None]]
    receipts: dict[tuple[str, tuple[str, ...]], PollReceipt] = field(default_factory=dict)
    uncertain: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)

    async def ensure_recorded(self, receipt: PollReceipt) -> None:
        if not receipt.recorded:
            await self.record_message(self.chat_id, self.sender_id, receipt.message_id,
                                      receipt.content, POLL_REASON)
            receipt.recorded = True

    async def finish_recording(self) -> None:
        """Drain confirmed side effects before the caller releases its reply lock."""
        for receipt in self.receipts.values():
            await self.ensure_recorded(receipt)

    async def send(self, question: str, options: list[str]) -> str:
        key = (question, tuple(options))
        if key in self.receipts:
            receipt = self.receipts[key]
            await self.ensure_recorded(receipt)
            return receipt.content
        if key in self.uncertain:
            raise RuntimeError("Poll delivery is uncertain; do not resend or claim success")
        media = InputMediaPoll(Poll(
            id=randbits(60), hash=0, question=TextWithEntities(question, []),
            answers=[PollAnswer(TextWithEntities(value, []), bytes([i])) for i, value in enumerate(options)],
            public_voters=True, multiple_choice=True, quiz=False, closed=False,
        ))
        self.uncertain.add(key)
        try:
            async with asyncio.timeout(POLL_SEND_TIMEOUT):
                sent = await self.client.send_file(self.chat_id, media)
        except asyncio.CancelledError as error:
            raise asyncio.CancelledError(
                "Poll delivery is uncertain after cancellation; do not resend or claim success."
            ) from error
        except TimeoutError as error:
            raise TimeoutError("Poll delivery is uncertain after the send timeout; do not resend") from error
        # Cancellation and transport exceptions may follow an accepted request.
        # Keep the uncertainty latch unless Telegram explicitly rejected it.
        except Exception as error:
            if isinstance(error, RPCError) and error.code in {303, 400, 401, 403, 404, 406, 420}:
                self.uncertain.discard(key)
                raise
            raise RuntimeError("Poll delivery is uncertain after a transport failure; do not resend") from error
        if sent is None or type(getattr(sent, "id", None)) is not int or sent.id <= 0:
            raise RuntimeError("Poll delivery is uncertain: Telegram returned no confirmed message")
        placeholder = f"[Poll sent]\nQuestion: {question}\nOptions:\n" + "\n".join(
            f"{i}. {value}" for i, value in enumerate(options, 1)
        )
        # Retain confirmation before persistence: retries must never resend.
        receipt = self.receipts[key] = PollReceipt(sent.id, placeholder)
        self.uncertain.discard(key)
        await self.ensure_recorded(receipt)
        return receipt.content


@tool(args_schema=SendPollArgs)
async def send_poll(question: str, options: list[str], config: RunnableConfig) -> str:
    """Send a public, multiple-choice poll in the current Telegram chat.

    Use when the latest unhandled messages or current outreach intent ask for a poll.
    Supply a nonblank question (up to 300 characters) and 2–10 distinct nonblank
    options (up to 100 characters each). This is an open regular poll, not a quiz.
    Returns a plain-text message containing the sent question and numbered
    options. Review the tool result before calling finish_context on a later pass.
    The responder uses the collected results to compose its text reply.
    Repeated identical calls in one turn reuse the confirmed result. Distinct polls
    can be sent independently. Failures are ordinary tool observations. Report
    uncertain delivery as unconfirmed and avoid automatic retries.
    """
    context = config.get("configurable", {})
    runtime = context.get("poll_delivery")
    if (not isinstance(runtime, PollDeliveryRuntime)
            or type(context.get("current_chat_id")) is not int
            or context["current_chat_id"] == 0
            or context["current_chat_id"] != runtime.chat_id):
        raise ValueError("Poll delivery requires matching application chat and delivery runtime")
    return await runtime.send(question, options)
