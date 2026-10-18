"""Raw retrieval records for one ephemeral reply."""

from typing import Any, Literal

from typing_extensions import TypedDict


class ToolObservation(TypedDict):
    pass_number: int
    tool_name: str
    arguments: dict[str, Any]
    result_category: Literal["schedule", "worldview", "user_info", "retrieval"]
    status: Literal["success", "empty", "error", "timeout"]
    raw_result: Any
