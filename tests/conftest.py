"""Tests never spend money: every LLM call is replayed from the committed cassettes. Stored record fields are
encrypted with a key generated for this test run only."""

import os

from cryptography.fernet import Fernet

os.environ["LLM_MODE"] = "replay"
os.environ["PRIORAUTH_FIELD_KEY"] = Fernet.generate_key().decode()

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
