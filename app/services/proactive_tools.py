"""LangChain CRUD tools restricted to the application's current Telegram chat."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from app import repository
from app.services.proactive_schemas import ProactiveScheduleIn, schedule_out, validated_schedule_data


class CreateScheduleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    cron_expression: str
    intent: str
    repeat_times: int | None = Field(..., strict=True, gt=0, le=99999)
    timezone: str = "Asia/Singapore"


class UpdateScheduleArgs(CreateScheduleArgs):
    schedule_id: int = Field(..., strict=True, gt=0)


class DeleteScheduleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: int = Field(..., strict=True, gt=0)


class SetScheduleEnabledArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: int = Field(..., strict=True, gt=0)
    enabled: bool = Field(..., strict=True)


class ListScheduleArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _current_chat_id(config: RunnableConfig) -> int:
    chat_id = config.get("configurable", {}).get("current_chat_id")
    if type(chat_id) is not int or chat_id == 0:
        raise ValueError("A valid current Telegram chat ID is required from application runtime context")
    return chat_id


async def _validated_data(
    config: RunnableConfig, name: str, cron_expression: str, intent: str,
    repeat_times: int | None, timezone: str,
) -> dict:
    chat_id = _current_chat_id(config)
    # Lazy imports keep tools importable while Telegram is still importing llm.
    from app.telegram.client import client

    return await validated_schedule_data(ProactiveScheduleIn(
        name=name, chat_id=chat_id, cron_expression=cron_expression, intent=intent,
        repeat_times=repeat_times, timezone=timezone, enabled=True,
    ), client.get_entity)


@tool(args_schema=CreateScheduleArgs)
async def create_proactive_schedule(
    name: str, cron_expression: str, intent: str, repeat_times: int | None,
    config: RunnableConfig, timezone: str = "Asia/Singapore",
) -> dict:
    """Create an enabled proactive reach-out in the current chat only.

    cron_expression is five-field cron (minute hour day month weekday), in the
    supplied IANA timezone. Set repeat_times=1 for a one-time reminder, a positive
    integer for a finite recurring schedule, or null for unlimited recurrence.
    intent must contain enough conversation context for a future Rachel to know
    what to send. Returns the saved schedule, its ID, counters and next run.
    """
    data = await _validated_data(config, name, cron_expression, intent, repeat_times, timezone)
    from app.services import proactive

    row = await proactive.apply_schedule_mutation(repository.create_proactive_schedule(data))
    return schedule_out(row).model_dump(mode="json")


@tool(args_schema=ListScheduleArgs)
async def list_proactive_schedules(config: RunnableConfig) -> list[dict]:
    """List all proactive reach-outs in the current chat, including disabled and exhausted ones.

    Returns schedule IDs, names, intent, cron/timezone, repeat allowance and
    attempts consumed, enabled state, last run/status/error and next run time.
    Use these IDs and details to identify a schedule before updating, toggling or deleting.
    """
    rows = await repository.get_proactive_schedules(chat_id=_current_chat_id(config))
    return [schedule_out(row).model_dump(mode="json") for row in rows]


@tool(args_schema=UpdateScheduleArgs)
async def update_proactive_schedule(
    schedule_id: int, name: str, cron_expression: str, intent: str,
    repeat_times: int | None, config: RunnableConfig, timezone: str = "Asia/Singapore",
) -> dict:
    """Replace a proactive schedule's editable details in the current chat and enable it.

    Supply the schedule ID from a current-chat listing and all desired details.
    Attempts already consumed (repeat_completed) are preserved. repeat_times is
    the TOTAL allowance: to allow N further attempts set repeat_completed + N;
    null removes the limit. The schedule cannot be moved to another chat.
    """
    data = await _validated_data(config, name, cron_expression, intent, repeat_times, timezone)
    from app.services import proactive

    row = await proactive.apply_schedule_mutation(repository.update_proactive_schedule(
        schedule_id, data, chat_id=_current_chat_id(config),
    ))
    if row is None:
        raise ValueError("Proactive schedule not found in the current chat")
    return schedule_out(row).model_dump(mode="json")


@tool(args_schema=SetScheduleEnabledArgs)
async def set_proactive_schedule_enabled(
    schedule_id: int, enabled: bool, config: RunnableConfig,
) -> dict:
    """Enable or disable one proactive reach-out in the current chat.

    Obtain the schedule ID from a current-chat listing; never guess an ID.
    Set enabled=false to pause future executions or true to resume them.
    Timing, intent, repeat allowance and consumed attempts are preserved.
    Enabling an exhausted schedule does not grant more attempts or schedule a run.
    Disabling does not cancel an execution already underway. Returns the saved
    schedule, enabled state, counters and next run time.
    """
    chat_id = _current_chat_id(config)
    from app.services import proactive

    row = await proactive.apply_schedule_mutation(repository.update_proactive_schedule(
        schedule_id, {"enabled": enabled}, chat_id=chat_id,
    ))
    if row is None:
        raise ValueError("Proactive schedule not found in the current chat")
    return schedule_out(row).model_dump(mode="json")


@tool(args_schema=DeleteScheduleArgs)
async def delete_proactive_schedule(schedule_id: int, config: RunnableConfig) -> dict:
    """Cancel and delete a proactive reach-out in the current chat using its schedule ID.

    Removes future scheduled executions. Obtain the ID from a current-chat
    listing; never guess an ID. Returns confirmation only after successful deletion.
    """
    chat_id = _current_chat_id(config)
    from app.services import proactive

    if not await proactive.apply_schedule_mutation(
        repository.delete_proactive_schedule(schedule_id, chat_id=chat_id),
        delete_schedule_id=schedule_id,
    ):
        raise ValueError("Proactive schedule not found in the current chat")
    return {"deleted": True, "schedule_id": schedule_id}


PROACTIVE_TOOLS = [
    create_proactive_schedule, list_proactive_schedules,
    update_proactive_schedule, set_proactive_schedule_enabled, delete_proactive_schedule,
]
