"""Spans really reach Jaeger over OTLP/HTTP. Runs only when PRIORAUTH_TEST_JAEGER_URL is set (CI runs a Jaeger
service; locally: make up). Expects OTLP/HTTP on port 4318 of the same host unless OTEL_EXPORTER_OTLP_ENDPOINT says
otherwise."""

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from priorauth.api.fhir import case_to_bundle
from priorauth.obs import tracing
from tests.test_api import api, post, wait_done
from tests.test_jobs import DEV

JAEGER = os.environ.get("PRIORAUTH_TEST_JAEGER_URL")
pytestmark = pytest.mark.skipif(not JAEGER, reason="PRIORAUTH_TEST_JAEGER_URL is not set")


class Switch(SpanProcessor):
    """Forward spans to the OTLP exporter only while this test runs (the global provider outlives the test)."""

    def __init__(self, inner: SpanProcessor) -> None:
        self.inner, self.on = inner, True

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if self.on:
            self.inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        if self.on:
            self.inner.on_end(span)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def test_job_spans_are_exported_to_jaeger(tmp_path: Path) -> None:
    assert JAEGER is not None
    host = urlparse(JAEGER).hostname
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", f"http://{host}:4318").rstrip("/") + "/v1/traces"
    switch = Switch(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    tracing.configure(switch)
    case, _ = DEV[0]

    async def scenario() -> str:
        async with api(tmp_path) as (client, _):
            case_id: str = (await post(client, case_to_bundle(case))).json()["case_id"]
            await wait_done(client, case_id)
            return case_id

    try:
        case_id = asyncio.run(scenario())
        switch.force_flush()
    finally:
        switch.on = False
    deadline = time.monotonic() + 20
    while True:
        r = httpx.get(
            f"{JAEGER}/api/traces", params={"service": tracing.SERVICE_NAME, "tags": f'{{"case.id":"{case_id}"}}'}
        )
        traces = r.json().get("data") or []
        if traces or time.monotonic() > deadline:
            break
        time.sleep(0.5)
    assert len(traces) == 1, r.text[:300]
    names = [s["operationName"] for s in traces[0]["spans"]]
    assert names.count("job.process") == 1 and names.count("llm.call") == 2 and "tool.call" in names
    llm = next(s for s in traces[0]["spans"] if s["operationName"] == "llm.call")
    tags = {t["key"]: t["value"] for t in llm["tags"]}
    assert {"tokens_in", "tokens_out", "cost_usd", "step"} <= set(tags)
