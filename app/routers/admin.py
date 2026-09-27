"""Admin HTTP API for managing Rachel without touching Telegram.

These endpoints reuse the same repository functions the Telethon handlers use,
so the HTTP API and the in-Telegram admin bot stay in sync.
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from apscheduler.triggers.cron import CronTrigger
from telethon import utils

from app import repository
from app import prompts
from app.prompts import USER_PROFILE_FIELDS
from app.services import userfacts
from app.services import worldview
from app.services import proactive
from app.telegram.client import client
from scripts.draw_graphs import GRAPHS, render_graphs

router = APIRouter()


# Workflow-node prompts that are NOT stored in the DB: they are imported
# directly from app.prompts and surfaced read-only in the dashboard so the
# whole pipeline's prompting is visible in one place. The responder &
# summarizer prompts are intentionally excluded here — they live in the DB
# and have their own editable endpoints above.
WORKFLOW_PROMPTS: list[tuple[str, str, str]] = [
    ("router", "Router / reply-gating", prompts.ROUTER_SYSTEM_PROMPT),
    ("context_fetcher", "Context fetcher", prompts.CONTEXT_FETCHER_SYSTEM_PROMPT),
    ("worldview_fact_extractor", "World-view: fact extractor", prompts.FACT_EXTRACTOR_SYSTEM_PROMPT),
    ("worldview_consolidation", "World-view: consolidation", prompts.CONSOLIDATION_SYSTEM_PROMPT),
    ("userfacts_fact_extractor", "User facts: fact extractor", prompts.USER_FACT_EXTRACTOR_SYSTEM_PROMPT),
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
    chat_name: str | None = None


class SummaryOut(BaseModel):
    chat_id: int
    summary: str | None


class UserProfileOut(BaseModel):
    user_id: int
    profile: dict = {}


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
    if "{intent}" not in body.prompt:
        raise HTTPException(
            status_code=422,
            detail="Responder prompt must retain the {intent} placeholder; proactive outreach depends on it.",
        )
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


async def resolve_chat_name(
    chat_id: int,
    get_entity: Callable[[int], Awaitable[Any]],
) -> str | None:
    """Resolve a chat ID to its Telegram display name without failing the list."""
    try:
        entity = await get_entity(chat_id)
    except Exception:
        return None

    display_name = utils.get_display_name(entity).strip()
    return display_name or None


async def enrich_chats_with_names(
    chats: list[dict[str, Any]],
    get_entity: Callable[[int], Awaitable[Any]],
) -> list[dict[str, Any]]:
    """Return chat rows with names resolved concurrently from Telegram."""
    names = await asyncio.gather(
        *(resolve_chat_name(chat["chat_id"], get_entity) for chat in chats)
    )
    return [
        {**chat, "chat_name": name}
        for chat, name in zip(chats, names, strict=True)
    ]


@router.get("/list-chats", response_model=list[AllChats])
async def get_all_chat_ids() -> list[AllChats]:
    chats = await repository.get_all_chats()
    chats = await enrich_chats_with_names(chats, client.get_entity)
    return [AllChats(**row) for row in chats]


# --- proactive outreach --------------------------------------------------


class ProactiveScheduleIn(BaseModel):
    name: str
    chat_id: int
    cron_expression: str
    timezone: str = "Asia/Singapore"
    intent: str
    enabled: bool = True


class ProactiveEnabledIn(BaseModel):
    enabled: bool


class ProactiveScheduleOut(ProactiveScheduleIn):
    id: int
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None = None


def _schedule_out(row: dict) -> ProactiveScheduleOut:
    return ProactiveScheduleOut(
        **row, next_run_at=proactive.next_run_at(row["id"])
    )


async def _validated_schedule_data(body: ProactiveScheduleIn) -> dict:
    name = body.name.strip()
    intent = body.intent.strip()
    cron_expression = " ".join(body.cron_expression.split())
    timezone_name = body.timezone.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be empty")
    if not intent:
        raise HTTPException(status_code=422, detail="intent must not be empty")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail=f"Invalid IANA timezone: {timezone_name!r}")
    try:
        CronTrigger.from_crontab(cron_expression, timezone=zone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid five-field cron expression: {exc}")
    try:
        await client.get_entity(body.chat_id)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Rachel cannot access Telegram chat {body.chat_id}: {exc}",
        )
    return {
        "name": name,
        "chat_id": body.chat_id,
        "cron_expression": cron_expression,
        "timezone": timezone_name,
        "intent": intent,
        "enabled": body.enabled,
    }


@router.get("/proactive-schedules", response_model=list[ProactiveScheduleOut])
async def list_proactive_schedules() -> list[ProactiveScheduleOut]:
    return [_schedule_out(row) for row in await repository.get_proactive_schedules()]


@router.post("/proactive-schedules", response_model=ProactiveScheduleOut, status_code=201)
async def create_proactive_schedule(body: ProactiveScheduleIn) -> ProactiveScheduleOut:
    row = await repository.create_proactive_schedule(await _validated_schedule_data(body))
    proactive.sync_schedule_job(row)
    return _schedule_out(row)


@router.put("/proactive-schedules/{schedule_id}", response_model=ProactiveScheduleOut)
async def update_proactive_schedule(
    schedule_id: int, body: ProactiveScheduleIn
) -> ProactiveScheduleOut:
    row = await repository.update_proactive_schedule(
        schedule_id, await _validated_schedule_data(body)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Proactive schedule not found")
    proactive.sync_schedule_job(row)
    return _schedule_out(row)


@router.delete("/proactive-schedules/{schedule_id}", status_code=204)
async def delete_proactive_schedule(schedule_id: int) -> None:
    if not await repository.delete_proactive_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="Proactive schedule not found")
    proactive.remove_schedule_job(schedule_id)


@router.patch(
    "/proactive-schedules/{schedule_id}/enabled", response_model=ProactiveScheduleOut
)
async def set_proactive_schedule_enabled(
    schedule_id: int, body: ProactiveEnabledIn
) -> ProactiveScheduleOut:
    row = await repository.update_proactive_schedule(
        schedule_id, {"enabled": body.enabled}
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Proactive schedule not found")
    proactive.sync_schedule_job(row)
    return _schedule_out(row)


@router.post("/proactive-schedules/{schedule_id}/run", response_model=ProactiveScheduleOut)
async def run_proactive_schedule(schedule_id: int) -> ProactiveScheduleOut:
    if await repository.get_proactive_schedule(schedule_id) is None:
        raise HTTPException(status_code=404, detail="Proactive schedule not found")
    if proactive.is_schedule_running(schedule_id):
        raise HTTPException(status_code=409, detail="This schedule is already running")
    if not await proactive.run_schedule(schedule_id):
        if proactive.is_schedule_running(schedule_id):
            raise HTTPException(status_code=409, detail="This schedule is already running")
        raise HTTPException(status_code=503, detail="Proactive scheduler is not accepting runs")
    row = await repository.get_proactive_schedule(schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Proactive schedule was deleted while running")
    return _schedule_out(row)


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


# Free-form user facts live in the Graphiti knowledge graph (one Neo4j
# group_id per user), so the only admin operations are ADD (ingest new fact
# episodes — same path as the pipeline's ingest_node) and GET (dump every
# episode in the user's partition). There is no edit/delete: Graphiti's own
# dedup/temporal conflict resolution supersedes old facts on ingest.


class UserFactsOut(BaseModel):
    user_id: int
    facts: list[str]


class UserFactsIn(BaseModel):
    facts: list[str]


@router.get("/user-facts/{user_id}", response_model=UserFactsOut)
async def read_user_facts(user_id: int) -> UserFactsOut:
    """Every fact episode stored for this user, oldest first."""
    try:
        facts = await userfacts.get_user_facts(user_id)
    except Exception as e:  # noqa: BLE001 — surface graph errors to the client
        raise HTTPException(status_code=502, detail=f"Failed to read user facts: {e}")
    return UserFactsOut(user_id=user_id, facts=facts)


@router.post("/user-facts/{user_id}", response_model=UserFactsOut)
async def add_user_facts(user_id: int, body: UserFactsIn) -> UserFactsOut:
    """Ingest new facts for this user. Slow: each fact is several LLM round-trips."""
    facts = [f.strip() for f in body.facts if f.strip()]
    if not facts:
        raise HTTPException(status_code=422, detail="No non-empty facts provided")
    try:
        await userfacts.add_user_facts(user_id, facts)
    except Exception as e:  # noqa: BLE001 — surface graph errors to the client
        raise HTTPException(status_code=502, detail=f"Failed to ingest user facts: {e}")
    return UserFactsOut(user_id=user_id, facts=facts)


# World-view facts live in the Graphiti knowledge graph (single "worldview"
# group_id), so — exactly like user facts — the only admin operations are ADD
# (ingest new fact episodes, same path as the pipeline's ingest_node) and GET
# (dump every episode in the partition). There is no edit/delete: Graphiti's
# own dedup/temporal conflict resolution supersedes old facts on ingest.


class WorldviewFactsOut(BaseModel):
    facts: list[str]


class WorldviewFactsIn(BaseModel):
    facts: list[str]


@router.get("/worldview-facts", response_model=WorldviewFactsOut)
async def read_worldview_facts() -> WorldviewFactsOut:
    """Every fact episode stored in the world view, oldest first."""
    try:
        facts = await worldview.get_worldview_facts()
    except Exception as e:  # noqa: BLE001 — surface graph errors to the client
        raise HTTPException(status_code=502, detail=f"Failed to read world-view facts: {e}")
    return WorldviewFactsOut(facts=facts)


@router.post("/worldview-facts", response_model=WorldviewFactsOut)
async def add_worldview_facts(body: WorldviewFactsIn) -> WorldviewFactsOut:
    """Ingest new world-view facts. Slow: each fact is several LLM round-trips."""
    facts = [f.strip() for f in body.facts if f.strip()]
    if not facts:
        raise HTTPException(status_code=422, detail="No non-empty facts provided")
    try:
        await worldview.add_worldview_facts(facts)
    except Exception as e:  # noqa: BLE001 — surface graph errors to the client
        raise HTTPException(status_code=502, detail=f"Failed to ingest world-view facts: {e}")
    return WorldviewFactsOut(facts=facts)


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


# --- llm models ----------------------------------------------------------
# A catalog of "<provider>/<model_name>" strings plus which one is active per
# role (main / small / embedding). Switching an active model rebuilds the LLM
# clients at runtime (no restart) — see repository.set_active_model.

MODEL_ROLES = ("main", "small", "embedding")
ModelRole = Literal["main", "small", "embedding"]


class ModelOut(BaseModel):
    id: int
    model_string: str


class ModelIn(BaseModel):
    model_string: str


class ActiveModelIn(BaseModel):
    model_string: str


@router.get("/models", response_model=list[ModelOut])
async def list_models() -> list[ModelOut]:
    return [ModelOut(**m) for m in await repository.get_all_models()]


@router.post("/models", response_model=ModelOut, status_code=201)
async def create_model(body: ModelIn) -> ModelOut:
    model_string = body.model_string.strip()
    if not model_string or "/" not in model_string:
        raise HTTPException(
            status_code=422, detail="model_string must be '<provider>/<model_name>'"
        )
    return ModelOut(**await repository.add_model(model_string))


@router.delete("/models", status_code=204)
async def delete_model(model_string: str) -> None:
    """Delete a catalog entry. The '<provider>/<model>' string contains a slash,
    so it's passed as a query param (?model_string=...) rather than a path arg."""
    await repository.delete_model(model_string)


@router.get("/active-models")
async def read_active_models() -> dict[str, str]:
    return await repository.get_active_models()


@router.put("/active-models/{role}")
async def set_active_model(role: ModelRole, body: ActiveModelIn) -> dict[str, str]:
    model_string = body.model_string.strip()
    catalog = {m["model_string"] for m in await repository.get_all_models()}
    if model_string not in catalog:
        raise HTTPException(
            status_code=422, detail=f"model_string {model_string!r} is not in the catalog"
        )
    await repository.set_active_model(role, model_string)
    return await repository.get_active_models()


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
