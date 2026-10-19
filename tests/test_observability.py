"""Tracing and logs: the expected spans exist, and no record content reaches a log line or a span."""

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from priorauth.api.fhir import case_to_bundle
from priorauth.data.schema import Case, Gold
from priorauth.obs import logs, tracing
from priorauth.pipeline import Extraction
from tests.test_api import api, one_per_trap, post, wait_done


def test_attributes_outside_the_allowlist_are_refused() -> None:
    assert tracing.attrs(tokens_in=3, llm__model="claude-haiku-5-5") == {
        "tokens_in": 3,
        "llm.model": "claude-haiku-5-5",
    }
    with pytest.raises(tracing.ForbiddenAttribute):
        tracing.attrs(quote="Low back pain for 9 weeks")
    with pytest.raises(tracing.ForbiddenAttribute):
        tracing.attrs(tool__name="x" * 200)
    with pytest.raises(tracing.ForbiddenAttribute):
        tracing.attrs(step="1")


def test_a_failing_span_records_the_exception_type_only(spans: InMemorySpanExporter) -> None:
    with pytest.raises(ValueError), tracing.span("job.process", job__id="j1"):
        raise ValueError("Patient Rosa Zielinski, MRN SYN-0000001")
    [s] = spans.get_finished_spans()
    assert s.status.description == "ValueError" and dict(s.attributes or {})["error.kind"] == "ValueError"
    assert s.events == ()


def _job_tree(spans: InMemorySpanExporter) -> dict[str, list[Any]]:
    by_name: dict[str, list[Any]] = {}
    for s in spans.get_finished_spans():
        by_name.setdefault(s.name, []).append(s)
    return by_name


def test_one_span_per_job_with_llm_and_tool_children(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    case, gold = one_per_trap()[0]

    async def scenario() -> None:
        async with api(tmp_path) as (client, _):
            r = await post(client, case_to_bundle(case))
            await wait_done(client, r.json()["case_id"])

    asyncio.run(scenario())
    tree = _job_tree(spans)
    [job] = tree["job.process"]
    assert job.attributes["job.outcome"] == gold.expected_decision and job.attributes["job.attempt"] == 1
    llm_calls = [s for s in tree["llm.call"] if s.parent.span_id == job.context.span_id]
    assert [s.attributes["step"] for s in llm_calls] == [0, 1]
    for s in llm_calls:
        assert {"tokens_in", "tokens_out", "cost_usd", "step", "llm.model"} <= set(s.attributes)
        assert s.attributes["tokens_in"] > 0 and s.attributes["cost_usd"] > 0
    tools = [s for s in tree["tool.call"] if s.parent.span_id == job.context.span_id]
    assert {s.attributes["tool.name"] for s in tools} == {
        "list_pages",
        "read_page",
        "search_policy",
        "record_finding",
        "finish",
    }
    assert job.attributes["llm.calls"] == 2
    assert job.attributes["cost_usd"] == pytest.approx(sum(s.attributes["cost_usd"] for s in llm_calls))


class Leaky:
    """A system that fails with record content in its exception message."""

    name = "leaky"

    async def extract(self, case: Case) -> Extraction:
        raise ValueError(f"cannot parse page: {case.pages[0].text[:200]} for {case.patient.name}")


def sensitive_strings(cases: list[tuple[Case, Gold]]) -> set[str]:
    out: set[str] = set()
    for case, gold in cases:
        p = case.patient
        out |= {p.name, p.mrn, p.member_id, p.birth_date.isoformat(), p.birth_date.strftime("%m/%d/%Y")}
        for page in case.pages:
            out |= {
                ln.strip() for ln in page.text.splitlines() if len(ln.strip()) >= 15 and "SYNTHETIC RECORD" not in ln
            }
        out |= {e.quote for g in gold.criteria.values() for e in g.evidence}
    return out


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.formatted = io.StringIO()
        self.json = logs.JsonFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.formatted.write(self.json.format(record) + "\n")


def test_no_record_content_in_logs_or_spans(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    cases = one_per_trap()
    capture = Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)  # the worst case: everything, including third-party DEBUG logs

    async def scenario() -> None:
        async with api(tmp_path / "ok", webhook_url="https://receiver.example/h", webhook_secret="s") as (
            client,
            hooks,
        ):
            ids = [
                (await post(client, case_to_bundle(c, c.case_id), key=c.case_id)).json()["case_id"] for c, _ in cases
            ]
            for case_id in ids:
                await wait_done(client, case_id)
            await client.get(f"/cases/{ids[0]}/result")
            for _ in range(100):
                if len(hooks) == len(ids):
                    break
                await asyncio.sleep(0.05)
            bad = case_to_bundle(cases[0][0])
            bad["entry"][0]["resource"]["birthDate"] = cases[0][0].pages[0].text[:40]  # content inside a 422
            assert (await post(client, bad, key="bad")).status_code == 422
            broken = b'{"resourceType": "Bundle", "note": "' + cases[0][0].pages[0].text[:60].encode() + b'"'
            r = await client.post(
                "/cases", content=broken, headers={"Idempotency-Key": "j", "content-type": "application/json"}
            )
            assert r.status_code == 422  # FastAPI's own validation error path (its spans are scanned too)
        (tmp_path / "leaky").mkdir()
        async with api(tmp_path / "leaky", system_factory=Leaky) as (client, _):
            case_id = (await post(client, case_to_bundle(cases[1][0]))).json()["case_id"]
            assert (await wait_done(client, case_id))["outcome"] == "PEND"

    (tmp_path / "ok").mkdir()
    try:
        asyncio.run(scenario())
    finally:
        root.removeHandler(capture)
        root.setLevel(old_level)

    secrets = sensitive_strings(cases)
    assert len(secrets) > 50 and capture.records and spans.get_finished_spans()
    names = {s.name for s in spans.get_finished_spans()}
    assert {"job.process", "llm.call", "tool.call", "POST /cases"} <= names  # ours and FastAPI's own spans
    log_text = "\n".join(
        [r.getMessage() for r in capture.records]
        + [json.dumps({k: str(v) for k, v in r.__dict__.items()}) for r in capture.records]
        + [capture.formatted.getvalue()]
    )
    span_text = "\n".join(
        json.dumps(
            {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
                "events": [(e.name, dict(e.attributes or {})) for e in s.events],
                "status": s.status.description,
                "resource": dict(s.resource.attributes),
            },
            default=str,
        )
        for s in spans.get_finished_spans()
    )
    leaked_logs = sorted(x for x in secrets if x in log_text)
    leaked_spans = sorted(x for x in secrets if x in span_text)
    assert leaked_logs == [] and leaked_spans == []
