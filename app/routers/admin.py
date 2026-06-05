"""Admin HTTP API for managing Rachel without touching Telegram.

These endpoints reuse the same repository functions the Telethon handlers use,
so the HTTP API and the in-Telegram admin bot stay in sync.
"""

from typing import Literal

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
    telegram_message_id: int

class UserNameOut(BaseModel):
    telegram_user_id: int
    first_name: str | None
    last_name: str | None
    username: str | None


class AllChats(BaseModel):
    chat_id: int
    message_count: int


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

@router.get("/users/names", response_model=list[UserNameOut])
async def read_user_names() -> list[UserNameOut]:
    return [UserNameOut(**u) for u in await repository.get_all_users()]


@router.get("/list-chats", response_model=list[AllChats])
async def get_all_chat_ids() -> list[AllChats]:
    return [AllChats(**row) for row in await repository.get_all_chats()]


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


# --- personality traits --------------------------------------------------

TraitValue = Literal["low", "medium", "high"]


class TraitOut(BaseModel):
    id: int
    name: str
    sort_order: int
    low_prompt: str
    medium_prompt: str
    high_prompt: str
    current_value: TraitValue


class TraitPatch(BaseModel):
    value: TraitValue


@router.get("/personality", response_model=list[TraitOut])
async def list_traits() -> list[TraitOut]:
    return [TraitOut(**t) for t in await repository.get_traits()]


@router.patch("/personality/{trait_id}", response_model=TraitOut)
async def update_trait(trait_id: int, body: TraitPatch) -> TraitOut:
    found = await repository.set_trait_value(trait_id, body.value)
    if not found:
        raise HTTPException(status_code=404, detail="Trait not found")
    traits = await repository.get_traits()
    trait = next((t for t in traits if t["id"] == trait_id), None)
    return TraitOut(**trait)


@router.post("/personality/reset", status_code=204)
async def reset_traits() -> None:
    await repository.reset_traits()
