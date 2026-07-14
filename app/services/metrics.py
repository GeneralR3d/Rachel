"""Prometheus metrics for the LLM pipeline.

Exposes two counters, scraped from the FastAPI ``/metrics`` endpoint mounted in
``app.main``:

- ``rachel_llm_calls_total{node}`` — every LLM attempt, per graph node. The
  denominator for an error-rate panel in Grafana.
- ``rachel_llm_errors_total{node,kind}`` — LLM failures, classified by cause.
  ``kind`` is a bounded set (see ``classify_llm_error``) so label cardinality
  stays small: e.g. ``upstream_504``, ``rate_limit_429``, ``upstream_5xx``,
  ``response_validation`` (the pydantic ``.../v/missing`` shape), ``timeout``,
  ``output_parse``, ``other``.

These are incremented at exactly the ``except`` sites in ``llm.py`` that today
already ``print`` the failure and fail open, so the counters are the ground
truth even for requests OpenRouter never bills/records on its side.
"""

import re

from prometheus_client import Counter

LLM_CALLS = Counter(
    "rachel_llm_calls_total",
    "LLM invocations attempted, per graph node",
    ["node"],
)

LLM_ERRORS = Counter(
    "rachel_llm_errors_total",
    "LLM invocation failures, per graph node and classified cause",
    ["node", "kind"],
)

# Matches an HTTP status embedded in an OpenRouter error envelope, e.g. the
# ResponseValidationError body ``{'error': {..., 'code': 504}}`` from an upstream
# gateway timeout. This is how a 504/429 surfaces even when the exception type is
# a pydantic validation error rather than an APIStatusError.
_CODE_RE = re.compile(r"['\"]code['\"]\s*:\s*(\d{3})\b")


def _status_from_code(code: int) -> str:
    if code == 429:
        return "rate_limit_429"
    if code == 504:
        return "upstream_504"
    if 500 <= code <= 599:
        return "upstream_5xx"
    if 400 <= code <= 499:
        return f"client_{code}"
    return "other"


def classify_llm_error(exc: BaseException) -> str:
    """Map an exception from an LLM call to a low-cardinality ``kind`` label.

    Checks, in order: asyncio timeout, LangChain output-parser failures, an
    explicit HTTP ``status_code`` on the exception (openai APIStatusError), an
    HTTP code embedded in the error-body text (OpenRouter envelopes surfaced as
    pydantic ValidationErrors — this is the 504/429 case), then the pydantic
    ``response_validation`` shape, falling back to ``other``."""
    import asyncio

    from langchain_core.exceptions import OutputParserException

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, OutputParserException):
        return "output_parse"

    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return _status_from_code(status)

    text = str(exc)
    match = _CODE_RE.search(text)
    if match:
        return _status_from_code(int(match.group(1)))

    if "validation error" in text.lower() or type(exc).__name__ == "ResponseValidationError":
        return "response_validation"

    return "other"


def record_llm_error(node: str, exc: BaseException) -> str:
    """Increment ``rachel_llm_errors_total`` for ``exc`` at ``node`` and return
    the classified ``kind`` (so callers can fold it into their log line)."""
    kind = classify_llm_error(exc)
    LLM_ERRORS.labels(node=node, kind=kind).inc()
    return kind


def record_llm_empty_output(node: str) -> str:
    """Count a structured-output call that returned ``None`` (no parsed object).

    LangChain returns ``None`` — rather than raising — when the model emits a
    payload that fails to validate against the requested schema (e.g. a null or
    a dropped required field). That bypasses ``record_llm_error``'s ``except``
    sites entirely, so without this the failure goes uncounted (and, before the
    ``is None`` guards, surfaced only as a downstream ``AttributeError``). It is
    a genuine response-shape failure, so it's bucketed as ``response_validation``
    — the same ``kind`` a pydantic ``.../v/missing`` error maps to."""
    kind = "response_validation"
    LLM_ERRORS.labels(node=node, kind=kind).inc()
    return kind
