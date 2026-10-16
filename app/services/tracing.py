"""Trace boundaries shared by reply and detached memory workflows."""

import json
from contextlib import contextmanager
from typing import Iterator

from openinference.instrumentation import using_attributes
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
