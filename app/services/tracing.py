"""Trace boundaries shared by reply and detached memory workflows."""

import json
import math
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from openinference.instrumentation import using_attributes
from openinference.semconv.trace import SpanAttributes
from opentelemetry import context, trace


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
def traced_jev_decision(
    model: str, state: str, question: dict[str, Any]
) -> Iterator[Callable[[dict[str, Any], float, bool], None]]:
    """Trace a JEV call, including its validated result or fail-open error."""
    with trace.get_tracer(__name__).start_as_current_span("JEV decision") as span:
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
