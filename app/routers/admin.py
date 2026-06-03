"""Admin HTTP API for managing Rachel without touching Telegram.

These endpoints reuse the same repository functions the Telethon handlers use,
so the HTTP API and the in-Telegram admin bot stay in sync.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import repository

router = APIRouter()


class SystemPromptIn(BaseModel):
    prompt: str


class SystemPromptOut(BaseModel):
    prompt: str


class HistoryItem(BaseModel):
    sender: str
    content: str
    message_id: int


class SummaryOut(BaseModel):
    chat_id: int
    summary: str | None


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/system-prompt", response_model=SystemPromptOut)
async def read_system_prompt() -> SystemPromptOut:
    prompt = await repository.get_system_prompt()
    if prompt is None:
        raise HTTPException(status_code=404, detail="System prompt not seeded")
    return SystemPromptOut(prompt=prompt)


@router.put("/system-prompt", response_model=SystemPromptOut)
async def update_system_prompt(body: SystemPromptIn) -> SystemPromptOut:
    await repository.set_system_prompt(body.prompt)
    return SystemPromptOut(prompt=body.prompt)


@router.get("/history/{chat_id}", response_model=list[HistoryItem])
async def read_history(chat_id: int) -> list[HistoryItem]:
    return [HistoryItem(**item) for item in await repository.get_history(chat_id)]


@router.delete("/history/{chat_id}", status_code=204)
async def delete_chat_history(chat_id: int) -> None:
    await repository.clear_history(chat_id)


@router.get("/summary/{chat_id}", response_model=SummaryOut)
async def read_summary(chat_id: int) -> SummaryOut:
    return SummaryOut(chat_id=chat_id, summary=await repository.get_summary(chat_id))


@router.delete("/summary/{chat_id}", status_code=204)
async def delete_chat_summary(chat_id: int) -> None:
    await repository.delete_summary(chat_id)
