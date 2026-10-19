"""Retries with backoff and jitter, timeouts, circuit breaker, transient requeue and the daily cost cap."""

import asyncio
import random
from pathlib import Path
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
from sqlalchemy import select

from priorauth.agent.loop import AgentSystem
from priorauth.api.fhir import case_to_bundle
from priorauth.jobs.db import JobRow
from priorauth.jobs.worker import DailyBudget, WorkerPool
from priorauth.llm.cassette import CassetteStore
from priorauth.llm.client import LLMClient, LLMMode, SpendLedger
from priorauth.llm.fake import NO_CASSETTES, OracleTransport, fake_client
from priorauth.llm.pricing import HAIKU
from priorauth.llm.resilience import CircuitBreaker, CircuitOpen, ResilientTransport, RetriesExhausted, classify
from priorauth.pipeline import run_case
from tests.test_api import api, post, wait_done
from tests.test_jobs import DEV, queue_for, submit

OK = {"type": "message", "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}


class StatusError(Exception):
    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"http {status}")
        self.status_code = status
        self.response = httpx.Response(status, headers={"retry-after": retry_after} if retry_after else {})


class Flaky:
    def __init__(self, failures: list[BaseException | float]) -> None:
        self.failures = list(failures)  # an exception to raise, or seconds to hang
        self.calls = 0

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.failures:
            f = self.failures.pop(0)
            if isinstance(f, float):
                await asyncio.sleep(f)
            else:
                raise f
        return OK


class Sleeps:
    def __init__(self) -> None:
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


def transport(inner: Flaky, **kw: Any) -> tuple[ResilientTransport, Sleeps]:
    sleeps = Sleeps()
    return ResilientTransport(inner, sleep=sleeps, rng=random.Random(0), **kw), sleeps


def test_classification() -> None:
    connection = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.example"))  # SDK 1.x: httpx2
    assert classify(StatusError(429)) == (True, None)
    assert classify(StatusError(529, retry_after="7")) == (True, 7.0)
    assert classify(StatusError(503)) == (True, None)
    assert classify(StatusError(400)) == (False, None)
    assert classify(StatusError(401)) == (False, None)
    assert classify(TimeoutError()) == (True, None)
    assert classify(connection) == (True, None)
    assert classify(ValueError("x")) == (False, None)


def test_429_and_5xx_are_retried_with_exponential_backoff_and_jitter() -> None:
    inner = Flaky([StatusError(429), StatusError(500), StatusError(503)])
    t, sleeps = transport(inner, base_delay_s=1.0, max_delay_s=30.0)
    assert asyncio.run(t.send({})) == OK and inner.calls == 4
    assert len(sleeps.waited) == 3
    for attempt, delay in enumerate(sleeps.waited, start=1):
        assert 0 <= delay <= 2 ** (attempt - 1)  # full jitter under an exponential ceiling
    assert len(set(sleeps.waited)) == 3  # jittered, not a fixed schedule


def test_retry_after_is_honoured() -> None:
    inner = Flaky([StatusError(429, retry_after="12")])
    t, sleeps = transport(inner, base_delay_s=0.1)
    asyncio.run(t.send({}))
    assert sleeps.waited == [12.0]


def test_client_errors_are_not_retried() -> None:
    inner = Flaky([StatusError(400)])
    t, sleeps = transport(inner)
    with pytest.raises(StatusError):
        asyncio.run(t.send({}))
    assert inner.calls == 1 and sleeps.waited == []


def test_timeouts_are_retried_then_exhausted() -> None:
    inner = Flaky([0.5, 0.5, 0.5])
    t, _ = transport(inner, max_attempts=3, timeout_s=0.05)
    with pytest.raises(RetriesExhausted, match="3 attempts failed, last error TimeoutError"):
        asyncio.run(t.send({}))
    assert inner.calls == 3


def test_circuit_breaker_opens_fails_fast_then_recovers() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=10, clock=lambda: now[0])
    inner = Flaky([StatusError(500)] * 4)
    t, _ = transport(inner, max_attempts=2, breaker=breaker)
    for _ in range(2):
        with pytest.raises(RetriesExhausted):
            asyncio.run(t.send({}))
    assert breaker.state == "open"
    calls = inner.calls
    with pytest.raises(CircuitOpen):
        asyncio.run(t.send({}))
    assert inner.calls == calls  # failed fast: the API was not called
    now[0] = 11.0
    assert breaker.state == "half-open"
    assert asyncio.run(t.send({})) == OK  # the trial call succeeds and closes the circuit
    assert breaker.state == "closed"


def test_transient_failure_requeues_the_job_with_a_delay(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with queue_for(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}", max_attempts=3) as queue:
            sub = await submit(queue, DEV[0][0], "k")
            down = ResilientTransport(Flaky([StatusError(503)] * 10), max_attempts=2, sleep=Sleeps())
            client = LLMClient(LLMMode.LIVE, CassetteStore(NO_CASSETTES), SpendLedger(path=None), down)
            system = AgentSystem("agent_down", HAIKU, "agent@v2", 0.10, llm=client)
            pool = WorkerPool(queue, system, concurrency=1, retry_delay_s=60)
            assert await pool.run_until_idle() == 1
            async with queue.sessions() as s:
                job = await s.get(JobRow, sub.job_id)
            assert job is not None
            assert (job.status, job.error, job.outcome) == ("queued", "RetriesExhausted", None)
            assert (job.available_at - job.updated_at).total_seconds() >= 30  # backoff, not an immediate retry
            assert await pool.run_until_idle() == 0  # not claimable before available_at

    asyncio.run(scenario())


def test_daily_cost_cap_stops_claiming_and_keeps_jobs_queued(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with queue_for(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}") as queue:
            for i in range(5):
                await submit(queue, DEV[i][0], f"k{i}")
            system = AgentSystem("agent_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(OracleTransport(DEV)))
            one_case = (await run_case(system, DEV[0][0])).usage.cost_usd  # fake usage priced at Haiku rates
            budget = DailyBudget(cap_usd=1.5 * one_case, reserve_per_job_usd=0)
            pool = WorkerPool(queue, system, concurrency=1, budget=budget)
            assert await pool.run_until_idle() == 2  # the cap is crossed by the second job, then claiming stops
            assert pool.paused_for_budget
            async with queue.sessions() as s:
                statuses = sorted((await s.execute(select(JobRow.status))).scalars())
            assert statuses == ["done", "done", "queued", "queued", "queued"]

    asyncio.run(scenario())


def test_cost_endpoint_reports_today(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, daily_cost_cap_usd=5.0) as (client, _):
            for case, _ in DEV[:3]:
                await wait_done(client, (await post(client, case_to_bundle(case), key=case.case_id)).json()["case_id"])
            report = (await client.get("/costs/daily")).json()
        assert report["daily_cap_usd"] == 5.0
        [today] = report["days"]
        assert today["jobs"] == 3 and today["llm_calls"] == 6 and today["cost_usd"] > 0

    asyncio.run(scenario())
