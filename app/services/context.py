"""Raw retrieval records and responder context for one ephemeral reply."""

from typing import Any, Literal

from typing_extensions import TypedDict


class ToolObservation(TypedDict):
    pass_number: int
    tool_name: str
    arguments: dict[str, Any]
    result_category: str
    status: Literal["success", "empty", "error", "timeout"]
    raw_result: Any


class ResponderContext(TypedDict):
    tool_results: str
    retrieval_status: str
