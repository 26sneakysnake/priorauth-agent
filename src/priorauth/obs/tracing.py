"""OpenTelemetry tracing: one span per job, one child span per LLM call and per tool call.

No record content ever goes into a span. Attributes go through an allowlist (`attrs`): identifiers that are opaque
(job and case UUIDs), counters, costs, model and tool names, error *kinds*. Exceptions are never recorded on spans
(their messages can quote input data): a failing span gets status ERROR with the exception type only.

Export: OTLP over HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. Jaeger, see docker-compose.yml); otherwise the
API is a no-op. Tests install an in-memory exporter.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

if TYPE_CHECKING:
    from priorauth.llm.client import LLMCall

SERVICE_NAME = "priorauth-agent"

# Every attribute a span may carry. Strings are limited to identifiers, names and kinds.
ALLOWED: dict[str, type | tuple[type, ...]] = {
    "job.id": str,
    "case.id": str,
    "job.attempt": int,
    "job.outcome": str,
    "job.error_kind": str,
    "procedure.code": str,
    "step": int,
    "llm.model": str,
    "llm.replayed": bool,
    "llm.stop_reason": str,
    "llm.latency_ms": (int, float),
    "llm.calls": int,
    "tokens_in": int,
    "tokens_out": int,
    "cache_read_tokens": int,
    "cache_write_tokens": int,
    "cost_usd": (int, float),
    "tool.name": str,
    "tool.is_error": bool,
    "tool.progress": bool,
    "error.kind": str,
}
MAX_STRING = 80


class ForbiddenAttribute(ValueError):
    pass


def attrs(**values: object) -> dict[str, str | bool | int | float]:
    """Validated span attributes: names from ALLOWED (dots written as double underscores), scalar types, short
    strings. Raises on anything else, so a leak is a test failure rather than a silent export."""
    out: dict[str, str | bool | int | float] = {}
    for raw, value in values.items():
        key = raw.replace("__", ".")
        if key not in ALLOWED:
            raise ForbiddenAttribute(f"span attribute {key!r} is not allowlisted")
        if value is None:
            continue
        if not isinstance(value, ALLOWED[key]) or (isinstance(value, str) and len(value) > MAX_STRING):
            raise ForbiddenAttribute(f"span attribute {key!r} has a disallowed value type or length")
        out[key] = value  # type: ignore[assignment]  # checked against ALLOWED above
    return out


def tracer() -> trace.Tracer:
    return trace.get_tracer("priorauth")


@contextmanager
def span(name: str, **values: object) -> Iterator[Span]:
    """A span that never records exception messages; on error its status holds the exception type only."""
    with tracer().start_as_current_span(
        name, attributes=attrs(**values), record_exception=False, set_status_on_exception=False
    ) as s:
        try:
            yield s
        except BaseException as e:
            s.set_status(Status(StatusCode.ERROR, type(e).__name__))
            s.set_attributes(attrs(error__kind=type(e).__name__))
            raise


def set_attrs(s: Span, **values: object) -> None:
    s.set_attributes(attrs(**values))


def configure(processor: SpanProcessor | None = None, environ: Mapping[str, str] = os.environ) -> TracerProvider | None:
    """Install a tracer provider: the given processor (tests), else OTLP/HTTP if an endpoint is configured."""
    if processor is None:
        if not environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            return None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        processor = BatchSpanProcessor(OTLPSpanExporter())
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):  # the global provider can be set once per process: extend it
        current.add_span_processor(processor)
        return current
    provider = TracerProvider(
        resource=Resource.create({"service.name": environ.get("OTEL_SERVICE_NAME", SERVICE_NAME)})
    )
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    return provider


def record_call(s: Span, call: "LLMCall") -> None:
    """Usage of one LLM call on its span: prompt tokens (uncached + cache reads + cache writes), output, cost."""
    u = call.usage
    set_attrs(
        s,
        tokens_in=u.prompt_tokens,
        tokens_out=u.output_tokens,
        cache_read_tokens=u.cache_read_tokens,
        cache_write_tokens=u.cache_write_tokens,
        cost_usd=call.cost_usd,
        llm__replayed=call.replayed,
        llm__latency_ms=call.latency_ms,
        llm__stop_reason=str(call.response.get("stop_reason") or "none")[:MAX_STRING],
    )
