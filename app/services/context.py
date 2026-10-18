"""Raw retrieval records and responder context for one ephemeral reply."""

import json
from typing import Any, Literal

from typing_extensions import TypedDict

from app.prompts import (
    RESPONDER_TOOL_RESULT_SECTIONS,
    USER_PROFILE_ATTRIBUTE_GUIDE,
    USER_PROFILE_FIELDS,
)


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


def _render_profile(profile: dict, show_unknown: bool = False) -> str:
    """Render profile slots in schema order, optionally exposing unknowns as NIL."""
    lines = []
    for key, label, _guide in USER_PROFILE_FIELDS:
        value = str(profile.get(key, "")).strip()
        if value:
            lines.append(f"- {label}: {value}")
        elif show_unknown:
            lines.append(f"- {label}: NIL")
    return "\n".join(lines)


def build_responder_context(observations: list[ToolObservation]) -> ResponderContext:
    """Combine accumulated observations with guidance at the model-call boundary.

    Empty user lookups still expose unknown profile slots. Other non-successes
    stay in retrieval status, never becoming successful facts. Unknown tool
    categories render by tool name, with optional guidance from the registry.
    """
    parts: dict[str, list[str]] = {key: [] for key in RESPONDER_TOOL_RESULT_SECTIONS}
    additional_results = []
    retrieval_status = []
    for observation in observations:
        result = observation["raw_result"]
        category = observation["result_category"]
        status = observation["status"]
        if status != "success":
            retrieval_status.append(json.dumps(observation, ensure_ascii=False))
        if category == "user_info" and status in ("success", "empty"):
            label = f"{result['name']} (id {result['user_id']})"
            if result["facts"]:
                parts["user_facts"].append(f"{label}:\n{result['facts']}")
            parts["user_profiles"].append(f"{label}:\n{_render_profile(result['profile'], show_unknown=True)}")
        elif status == "success":
            key = {"schedule": "schedule_context", "worldview": "world_view"}.get(category, category)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            if key in parts:
                parts[key].append(text)
            else:
                additional_results.append(f"{observation['tool_name']}:\n{text}")

    sections = [
        template.format(
            result="\n\n".join(parts[key]) or fallback,
            profile_attributes=USER_PROFILE_ATTRIBUTE_GUIDE,
        )
        for key, (template, fallback) in RESPONDER_TOOL_RESULT_SECTIONS.items()
    ]
    return {
        "tool_results": "\n\n".join([*sections, *additional_results]),
        "retrieval_status": "\n\n".join(retrieval_status),
    }
