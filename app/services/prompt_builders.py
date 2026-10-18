"""Pure assembly of complete system prompts and separate retrieval messages."""

from collections.abc import Collection, Sequence
import json

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import BaseTool

from app.prompts import (
    CONTEXT_FETCHER_TOOL_GUIDANCE,
    RESPONDER_TOOL_RESULT_SECTIONS,
    USER_PROFILE_ATTRIBUTE_GUIDE,
    USER_PROFILE_FIELDS,
)
from app.services.context import ResponderContext, ToolObservation


def format_tools(tools: Sequence[BaseTool]) -> str:
    """Render any list of LangChain tools as a bullet list for a prompt.

    Derived entirely from each tool's name, argument names and FULL docstring, so
    the prompt never hard-codes tool descriptions — the tools stay the one source
    of truth. Each entry is ``- name(args):`` followed by the complete docstring.
    """
    lines = []
    for t in tools:
        args = ", ".join(t.args.keys())
        doc = (t.description or "").strip()
        lines.append(f"- {t.name}({args}):\n{doc}")
    return "\n\n".join(lines)


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


def build_context_fetcher_prompt(
    *,
    system_prompt: str,
    tools: Sequence[BaseTool],
    available_categories: Collection[str],
    participants: str,
    current_datetime: str,
) -> str:
    """Render the complete fetcher prompt for explicitly available capabilities."""
    fields = {"participants": participants, "datetime": current_datetime}
    guidance = [
        text for category, text in CONTEXT_FETCHER_TOOL_GUIDANCE.items()
        if category in available_categories
    ]
    fields.update(tool_guidance="\n\n".join(guidance), tools=format_tools(tools))
    return system_prompt.format(**fields)


def build_responder_prompt(
    *,
    system_prompt: str,
    context: ResponderContext,
    personality_traits: str,
    communication_style: str,
    conversation_mood: str,
    current_summary: str,
    current_datetime: str,
) -> str:
    """Render the complete responder prompt from resolved runtime values."""
    fields = {
        "communication_style": communication_style,
        "current_summary": current_summary,
        "personality_traits": personality_traits,
        "conversation_mood": conversation_mood,
        "datetime": current_datetime,
    }
    fields["tool_results"] = context["tool_results"]
    template = ChatPromptTemplate.from_messages([("system", system_prompt)])
    return template.format_messages(**fields)[0].content


def build_context_fetcher_handoff(
    *,
    context_pass: int,
    max_context_passes: int,
    retrieval_request: str,
    observations: list[ToolObservation],
) -> str:
    """Serialize all prior observations as a separate fetcher handoff."""
    return json.dumps({
        "context_pass": context_pass,
        "max_context_passes": max_context_passes,
        "retrieval_request": retrieval_request,
        "previous_observations": observations,
    }, ensure_ascii=False)


def build_responder_loop_message(
    *,
    instructions: str,
    context_passes: int,
    max_context_passes: int,
    remaining_steps: int | None,
    context: ResponderContext,
) -> str:
    """Keep loop policy and serialized retrieval status in their own message."""
    return instructions + json.dumps({
        "context_passes": context_passes,
        "max_context_passes": max_context_passes,
        "remaining_steps": remaining_steps,
        "retrieval_status": context["retrieval_status"],
    }, ensure_ascii=False)
