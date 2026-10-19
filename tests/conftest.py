"""Tests never spend money: every LLM call is replayed from the committed cassettes."""

import os

os.environ["LLM_MODE"] = "replay"

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from priorauth.obs import tracing

SPANS = InMemorySpanExporter()
tracing.configure(SimpleSpanProcessor(SPANS))  # once per process: the global tracer provider cannot be replaced


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    SPANS.clear()
    yield SPANS
    SPANS.clear()
