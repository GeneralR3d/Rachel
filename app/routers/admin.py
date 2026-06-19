"""Admin HTTP API for managing Rachel without touching Telegram.

These endpoints reuse the same repository functions the Telethon handlers use,
so the HTTP API and the in-Telegram admin bot stay in sync.
"""

import asyncio
import base64
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import repository
from app import prompts
from app.prompts import USER_PROFILE_FIELDS
from scripts.draw_graphs import GRAPHS, render_graphs

router = APIRouter()


# Workflow-node prompts that are NOT stored in the DB: they are imported
# directly from app.prompts and surfaced read-only in the dashboard so the
# whole pipeline's prompting is visible in one place. The responder &
# summarizer prompts are intentionally excluded here — they live in the DB
# and have their own editable endpoints above.
WORKFLOW_PROMPTS: list[tuple[str, str, str]] = [
    ("router", "Router / reply-gating", prompts.ROUTER_SYSTEM_PROMPT),
    ("worldview_fact_extractor", "World-view: fact extractor", prompts.FACT_EXTRACTOR_SYSTEM_PROMPT),
    ("worldview_consolidation", "World-view: consolidation", prompts.CONSOLIDATION_SYSTEM_PROMPT),
    ("userfacts_fact_extractor", "User facts: fact extractor", prompts.USER_FACT_EXTRACTOR_SYSTEM_PROMPT),
    ("userfacts_consolidation", "User facts: consolidation", prompts.USER_FACT_CONSOLIDATION_SYSTEM_PROMPT),
    ("userprofile_extractor", "User profile: extractor", prompts.USER_PROFILE_EXTRACTOR_SYSTEM_PROMPT),
]


class SystemPromptIn(BaseModel):
    prompt: str


class SystemPromptOut(BaseModel):
    prompt: str


class SummarizerSystemPromptIn(BaseModel):
    prompt: str


class SummarizerSystemPromptOut(BaseModel):
    prompt: str


class HistoryItem(BaseModel):
    sender: str
    content: str
    telegram_message_id: int
    reason: str | None = None

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


class UserFactsOut(BaseModel):
    user_id: int
    facts: str


class UserProfileOut(BaseModel):
    user_id: int
    profile: dict = {}


class UserFactsIn(BaseModel):
    facts: str


class UserProfileIn(BaseModel):
    profile: dict


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/responder-system-prompt", response_model=SystemPromptOut)
async def read_system_prompt() -> SystemPromptOut:
    prompt = await repository.get_responder_system_prompt()
    if prompt is None:
        raise HTTPException(status_code=404, detail="responder System prompt not seeded")
    return SystemPromptOut(prompt=prompt)


@router.put("/responder-system-prompt", response_model=SystemPromptOut)
async def update_system_prompt(body: SystemPromptIn) -> SystemPromptOut:
    await repository.set_responder_system_prompt(body.prompt)
    return SystemPromptOut(prompt=body.prompt)


@router.get("/summarizer-system-prompt", response_model=SummarizerSystemPromptOut)
async def read_summarizer_system_prompt() -> SummarizerSystemPromptOut:
    prompt = await repository.get_summarizer_system_prompt()
    if prompt is None:
        raise HTTPException(status_code=404, detail="Summarizer system prompt not seeded")
    return SummarizerSystemPromptOut(prompt=prompt)


@router.put("/summarizer-system-prompt", response_model=SummarizerSystemPromptOut)
async def update_summarizer_system_prompt(body: SummarizerSystemPromptIn) -> SummarizerSystemPromptOut:
    await repository.set_summarizer_system_prompt(body.prompt)
    return SummarizerSystemPromptOut(prompt=body.prompt)

class WorkflowPromptOut(BaseModel):
    key: str
    label: str
    prompt: str


@router.get("/workflow-prompts", response_model=list[WorkflowPromptOut])
async def read_workflow_prompts() -> list[WorkflowPromptOut]:
    """Read-only view of every workflow-node prompt that is hard-coded in
    app.prompts (not stored in the DB). Surfaced so the dashboard can show the
    full pipeline's prompting; there is no setter — these are unmodifiable."""
    return [WorkflowPromptOut(key=k, label=label, prompt=text) for k, label, text in WORKFLOW_PROMPTS]


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


@router.get("/user-facts/{user_id}", response_model=UserFactsOut)
async def read_user_facts(user_id: int) -> UserFactsOut:
    return UserFactsOut(user_id=user_id, facts=await repository.get_user_facts(user_id))


@router.put("/user-facts/{user_id}", response_model=UserFactsOut)
async def update_user_facts(user_id: int, body: UserFactsIn) -> UserFactsOut:
    await repository.set_user_facts(user_id, body.facts)
    return UserFactsOut(user_id=user_id, facts=body.facts)


@router.delete("/user-facts/{user_id}", status_code=204)
async def delete_user_facts(user_id: int) -> None:
    await repository.delete_user_facts(user_id)


class ProfileFieldOut(BaseModel):
    key: str
    label: str


@router.get("/user-profile-fields", response_model=list[ProfileFieldOut])
async def list_profile_fields() -> list[ProfileFieldOut]:
    """The fixed profile slot schema, so clients can render every attribute
    (including empty ones) from a single source of truth."""
    return [ProfileFieldOut(key=key, label=label) for key, label, _guide in USER_PROFILE_FIELDS]


@router.get("/user-profile/{user_id}", response_model=UserProfileOut)
async def read_user_profile(user_id: int) -> UserProfileOut:
    return UserProfileOut(user_id=user_id, profile=await repository.get_user_profile(user_id))


@router.put("/user-profile/{user_id}", response_model=UserProfileOut)
async def update_user_profile(user_id: int, body: UserProfileIn) -> UserProfileOut:
    await repository.set_user_profile(user_id, body.profile)
    return UserProfileOut(user_id=user_id, profile=body.profile)


@router.delete("/user-profile/{user_id}", status_code=204)
async def delete_user_profile(user_id: int) -> None:
    await repository.delete_user_profile(user_id)


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


# --- architecture (LangGraph pipeline diagrams) --------------------------


class ArchitectureGraph(BaseModel):
    filename: str
    label: str
    png_base64: str


@router.get("/architecture", response_model=list[ArchitectureGraph])
async def read_architecture() -> list[ArchitectureGraph]:
    """Re-render every LangGraph pipeline to a PNG on each request and return
    them inline (base64) so the dashboard always shows the current graphs."""
    try:
        # draw_mermaid_png() does blocking network I/O — run off the event loop.
        rendered = await asyncio.to_thread(render_graphs)
    except Exception as e:  # noqa: BLE001 — surface the rendering failure to the client
        raise HTTPException(status_code=502, detail=f"Failed to render graphs: {e}")
    labels = {fn: label for fn, (label, _factory) in GRAPHS.items()}
    return [
        ArchitectureGraph(
            filename=fn,
            label=labels.get(fn, fn),
            png_base64=base64.b64encode(png).decode("ascii"),
        )
        for fn, png in rendered.items()
    ]
