"""Trace boundaries shared by reply and detached memory workflows."""

import json
import math
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from openinference.instrumentation import get_attributes_from_context, using_attributes
from openinference.instrumentation.langchain import get_current_span as get_langchain_span
from openinference.semconv.trace import SpanAttributes
from opentelemetry import context, trace
from opentelemetry.trace import StatusCode

from app.services.context import ToolObservation


@contextmanager
def traced_workflow(
    name: str,
    session_id: str | None,
    chat_id: int | None = None,
    chat_name: str | None = None,
) -> Iterator[None]:
    metadata = {"telegram_chat_id": chat_id} if chat_id is not None else {}
    if chat_name:
        metadata["telegram_chat_name"] = chat_name
    token = context.attach(context.Context())
    try:
        with using_attributes(session_id=session_id or "", metadata=metadata or None):
            with trace.get_tracer(__name__).start_as_current_span(name) as span:
                span.set_attribute("openinference.span.kind", "CHAIN")
                if session_id:
                    span.set_attribute("session.id", session_id)
                if metadata:
                    span.set_attribute("metadata", json.dumps(metadata, ensure_ascii=False))
                if chat_id is not None:
                    span.set_attribute("telegram.chat.id", chat_id)
                if chat_name:
                    span.set_attribute("telegram.chat.name", chat_name)
                yield
    finally:
        context.detach(token)


@contextmanager
def traced_tool_call(observation: ToolObservation, call_id: str | None = None) -> Iterator[None]:
    """Trace a direct lookup using its final observation, including cancellation.

    The caller updates the observation before exiting. An interrupted retrieval
    keeps its reserved timeout result, so even a cancelled call exports output.
    LangChain tools already have automatic spans and must not use this helper.
    """
    parent = get_langchain_span()
    parent_context = trace.set_span_in_context(parent) if parent is not None else None
    with trace.get_tracer(__name__).start_as_current_span(
        observation["tool_name"], context=parent_context,
    ) as span:
        span.set_attributes(dict(get_attributes_from_context()))
        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, "TOOL")
        span.set_attribute(SpanAttributes.TOOL_NAME, observation["tool_name"])
        if call_id:
            span.set_attribute(SpanAttributes.TOOL_ID, call_id)
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
        span.set_attribute(
            SpanAttributes.INPUT_VALUE,
            json.dumps(observation["arguments"], ensure_ascii=False, default=str),
        )
        span.set_attribute("tool.pass_number", observation["pass_number"])
        span.set_attribute("tool.result_category", observation["result_category"])
        try:
            yield
        finally:
            result = observation["raw_result"]
            status = observation["status"]
            span.set_attribute("tool.status", status)
            span.set_attribute(
                SpanAttributes.OUTPUT_MIME_TYPE,
                "text/plain" if isinstance(result, str) else "application/json",
            )
            span.set_attribute(
                SpanAttributes.OUTPUT_VALUE,
                result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str),
            )
            if status in {"error", "timeout"}:
                span.set_status(StatusCode.ERROR, str(result))
            else:
                span.set_status(StatusCode.OK)


@contextmanager
def traced_jev_decision(
    model: str, state: str, question: dict[str, Any]
) -> Iterator[Callable[[dict[str, Any], float, bool], None]]:
    """Trace a JEV call, including its validated result or fail-open error."""
    # LangChain's callback spans are not attached to the OpenTelemetry context.
    parent = get_langchain_span()
    parent_context = trace.set_span_in_context(parent) if parent is not None else None
    with trace.get_tracer(__name__).start_as_current_span("JEV decision", context=parent_context) as span:
        span.set_attribute(SpanAttributes.OPENINFERENCE_SPAN_KIND, "LLM")
        span.set_attribute(SpanAttributes.LLM_MODEL_NAME, model)
        span.set_attribute(SpanAttributes.LLM_REQUEST_MODEL_NAME, model)
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
        span.set_attribute(
            SpanAttributes.INPUT_VALUE,
            json.dumps({"state": state, "questions": {"should_reply": question}}, ensure_ascii=False),
        )
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        def record_result(response: dict[str, Any], probability: float, should_reply: bool) -> None:
            span.set_attribute(
                SpanAttributes.OUTPUT_VALUE,
                json.dumps({"probability": probability, "should_reply": should_reply}),
            )
            for response_key, attribute in (
                ("model", SpanAttributes.LLM_RESPONSE_MODEL_NAME),
                ("vendor", SpanAttributes.LLM_PROVIDER),
            ):
                value = response.get(response_key)
                if isinstance(value, str):
                    span.set_attribute(attribute, value)
                    if response_key == "model":
                        span.set_attribute(SpanAttributes.LLM_MODEL_NAME, value)

            usage = response.get("usage")
            if not isinstance(usage, dict):
                return
            for usage_key, attribute in (
                ("input_tokens", SpanAttributes.LLM_TOKEN_COUNT_PROMPT),
                ("output_tokens", SpanAttributes.LLM_TOKEN_COUNT_COMPLETION),
                ("total_tokens", SpanAttributes.LLM_TOKEN_COUNT_TOTAL),
                ("cost", SpanAttributes.LLM_COST_TOTAL),
            ):
                value = usage.get(usage_key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    span.set_attribute(attribute, value)

        try:
            yield record_result
        except Exception:
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({"should_reply": True}))
            raise
