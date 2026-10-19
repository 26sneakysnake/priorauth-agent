"""Job queue, worker pool and webhooks. Run on SQLite always, and on Postgres when PRIORAUTH_TEST_POSTGRES_URL is set
(CI runs a Postgres service; locally any reachable server works)."""

import asyncio
import json
import os
import random
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from priorauth.agent.loop import AgentSystem
from priorauth.api.fhir import case_to_bundle, parse_bundle
from priorauth.data.schema import Case, Gold
from priorauth.jobs import webhooks
from priorauth.jobs.db import Base, JobRow, WebhookDeliveryRow, create_schema, make_engine, sessions, utcnow
from priorauth.jobs.queue import IdempotencyConflict, JobQueue, Submitted
from priorauth.jobs.worker import WorkerPool
from priorauth.llm.fake import OracleTransport, fake_client
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import Extraction
from tests.test_data import load

POSTGRES = os.environ.get("PRIORAUTH_TEST_POSTGRES_URL")
BACKENDS = ["sqlite", *(["postgres"] if POSTGRES else [])]
DEV = load("dev")


class Clock:
    def __init__(self) -> None:
        self.now = utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(params=BACKENDS)
def db_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}"
    else:
        assert POSTGRES is not None
        yield POSTGRES


@asynccontextmanager
async def queue_for(url: str, **kwargs: Any) -> AsyncIterator[JobQueue]:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(engine)
    try:
        yield JobQueue(sessions(engine), **kwargs)
    finally:
        await engine.dispose()


async def submit(queue: JobQueue, case: Case, key: str) -> Submitted:
    parsed = parse_bundle(case_to_bundle(case), case_id=f"{key}-{case.case_id}")
    return await queue.submit(parsed, key, digest=key)


def agent(cases: list[tuple[Case, Gold]] = DEV, **kwargs: Any) -> AgentSystem:
    return AgentSystem("agent_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(OracleTransport(cases, **kwargs)))


class Blocking:
    """A system that never finishes: its worker can only be killed."""

    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def extract(self, case: Case) -> Extraction:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def count(queue: JobQueue, *where: Any) -> int:
    async with queue.sessions() as s:
        return int((await s.execute(select(func.count()).select_from(JobRow).where(*where))).scalar_one())


# --------------------------------------------------------------------------------------------------------------


def test_claim_query_uses_skip_locked_on_postgres_only() -> None:
    from sqlalchemy.dialects import postgresql, sqlite

    from priorauth.jobs.queue import claimable

    pg, lite = postgresql.dialect(), sqlite.dialect()  # type: ignore[no-untyped-call]
    assert "FOR UPDATE SKIP LOCKED" in str(claimable(utcnow()).compile(dialect=pg))
    assert "FOR UPDATE" not in str(claimable(utcnow()).compile(dialect=lite))


def test_concurrent_workers_never_claim_the_same_job(db_url: str) -> None:
    async def scenario() -> None:
        async with queue_for(db_url) as queue:
            for i in range(30):
                await submit(queue, DEV[i % 5][0], f"k{i}")
            claims = await asyncio.gather(*(queue.claim(f"w{i % 6}") for i in range(40)))
            ids = [c.job_id for c in claims if c is not None]
            assert len(ids) == 30 and len(set(ids)) == 30
            assert await count(queue, JobRow.status == "running") == 30

    asyncio.run(scenario())


def test_idempotent_submission(db_url: str) -> None:
    async def scenario() -> None:
        async with queue_for(db_url) as queue:
            parsed = parse_bundle(case_to_bundle(DEV[0][0]), case_id="c1")
            first = await queue.submit(parsed, "key-1", digest="d1")
            again = await queue.submit(parse_bundle(case_to_bundle(DEV[0][0]), case_id="c2"), "key-1", digest="d1")
            assert (again.case_id, again.job_id, again.created) == (first.case_id, first.job_id, False)
            with pytest.raises(IdempotencyConflict):
                await queue.submit(parse_bundle(case_to_bundle(DEV[1][0]), case_id="c3"), "key-1", digest="d2")
            racing = await asyncio.gather(
                *(
                    queue.submit(parse_bundle(case_to_bundle(DEV[2][0]), case_id=f"r{i}"), "key-2", "d3")
                    for i in range(5)
                )
            )
            assert len({r.job_id for r in racing}) == 1 and sum(r.created for r in racing) == 1
            assert await count(queue) == 2

    asyncio.run(scenario())


def test_crashed_worker_job_is_reclaimed_after_its_lease(db_url: str) -> None:
    async def scenario() -> None:
        async with queue_for(db_url, lease_seconds=0.4) as queue:
            sub = await submit(queue, DEV[0][0], "k")
            blocking = Blocking()
            crashed = WorkerPool(queue, blocking, concurrency=1, poll_s=0.05, worker_id="crashed")
            stop = asyncio.Event()
            runner = asyncio.create_task(crashed.run(stop))
            await asyncio.wait_for(blocking.started.wait(), timeout=5)
            for task in [runner, *crashed.tasks]:  # the process dies: no release, no result, no more heartbeats
                task.cancel()
            await asyncio.gather(runner, *crashed.tasks, return_exceptions=True)
            assert await count(queue, JobRow.status == "running", JobRow.locked_by == "crashed") == 1

            survivor = WorkerPool(queue, agent(), concurrency=1, worker_id="survivor")
            assert await survivor.run_until_idle() == 0  # the lease has not expired yet
            await asyncio.sleep(0.5)
            assert await survivor.run_until_idle() == 1
            async with queue.sessions() as s:
                job = await s.get(JobRow, sub.job_id)
            assert job is not None
            assert (job.status, job.attempts, job.outcome, job.locked_by) == (
                "done",
                2,
                DEV[0][1].expected_decision,
                None,
            )

    asyncio.run(scenario())


def test_a_worker_that_lost_its_lease_cannot_overwrite_the_result(db_url: str) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with queue_for(db_url, lease_seconds=30, clock=clock, webhook_url="https://hooks.example/x") as queue:
            sub = await submit(queue, DEV[0][0], "k")
            first = await queue.claim("slow")
            assert first is not None and first.attempts == 1
            clock.advance(31)
            second = await queue.claim("fast")
            assert second is not None and second.job_id == first.job_id and second.attempts == 2
            assert not await queue.heartbeat(first.job_id, "slow")
            from priorauth.pipeline import run_case

            run = await run_case(agent(), DEV[0][0])
            assert await queue.complete(second.job_id, "fast", run)
            assert not await queue.complete(first.job_id, "slow", run)  # the zombie's write is refused
            async with queue.sessions() as s:
                n_hooks = (await s.execute(select(func.count()).select_from(WebhookDeliveryRow))).scalar_one()
            assert n_hooks == 1
            assert sub.job_id == first.job_id

    asyncio.run(scenario())


def test_a_job_that_keeps_crashing_fails_closed_after_max_attempts(db_url: str) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with queue_for(
            db_url, lease_seconds=10, max_attempts=3, clock=clock, webhook_url="https://h.example"
        ) as queue:
            sub = await submit(queue, DEV[0][0], "k")
            for attempt in range(1, 4):
                job = await queue.claim(f"w{attempt}")
                assert job is not None and job.attempts == attempt
                clock.advance(11)
            assert await queue.claim("w4") is None
            async with queue.sessions() as s:
                job_row = await s.get(JobRow, sub.job_id)
                hook = (await s.execute(select(WebhookDeliveryRow))).scalar_one()
            assert job_row is not None
            assert (job_row.status, job_row.outcome, job_row.error) == ("failed", "PEND", "max_attempts: worker lost")
            assert json.loads(hook.payload_json)["outcome"] == "PEND"

    asyncio.run(scenario())


def test_worker_pool_respects_its_concurrency_limit(db_url: str) -> None:
    async def scenario() -> None:
        async with queue_for(db_url) as queue:
            for i in range(12):
                await submit(queue, DEV[i][0], f"k{i}")
            transport = OracleTransport(DEV, latency_s=0.05)
            active, peak = 0, 0
            original = transport.send

            async def tracked(request: dict[str, Any]) -> dict[str, Any]:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    return await original(request)
                finally:
                    active -= 1

            transport.send = tracked  # type: ignore[method-assign]
            system = AgentSystem("agent_fake", HAIKU, "agent@v2", 0.10, llm=fake_client(transport))
            pool = WorkerPool(queue, system, concurrency=3)
            assert await pool.run_until_idle() == 12
            assert peak == 3
            assert await count(queue, JobRow.status == "done") == 12

    asyncio.run(scenario())


# --------------------------------------------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------------------------------------------

SECRET = b"test-secret"


def test_signature_round_trip_and_replay_protection() -> None:
    body = b'{"event":"case.decided"}'
    header = webhooks.sign(SECRET, 1_700_000_000, body)
    assert webhooks.verify(SECRET, header, body, now=1_700_000_010)
    assert not webhooks.verify(SECRET, header, body + b" ", now=1_700_000_010)  # tampered body
    assert not webhooks.verify(b"other", header, body, now=1_700_000_010)  # wrong secret
    assert not webhooks.verify(SECRET, header, body, now=1_700_000_000 + 301)  # replayed later
    assert not webhooks.verify(SECRET, "garbage", body)


def test_backoff_grows_exponentially_with_jitter_and_a_cap() -> None:
    rng = random.Random(0)
    delays = [webhooks.backoff_seconds(n, base=2, cap=60, rng=rng) for n in range(1, 9)]
    for n, d in enumerate(delays, start=1):
        ceiling = min(60, 2 * 2 ** (n - 1))
        assert ceiling / 2 <= d <= ceiling
    assert max(delays) <= 60


def test_webhook_is_retried_with_backoff_until_delivered(db_url: str) -> None:
    async def scenario() -> None:
        clock = Clock()
        statuses = iter([500, 503])
        received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received.append(request)
            code = next(statuses, 200)
            if code == 503:
                raise httpx.ConnectError("receiver down")
            return httpx.Response(code)

        async with queue_for(db_url, clock=clock, webhook_url="https://hooks.example/decided") as queue:
            await submit(queue, DEV[0][0], "k")
            await WorkerPool(queue, agent(), concurrency=1).run_until_idle()
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            sender = webhooks.WebhookSender(queue.sessions, SECRET, client, base_delay_s=2, clock=clock)
            assert await sender.deliver_due() == 1  # 500
            assert await sender.deliver_due() == 0  # not due yet
            clock.advance(2)
            assert await sender.deliver_due() == 1  # connection error
            clock.advance(4)
            assert await sender.deliver_due() == 1  # delivered
            await client.aclose()
            async with queue.sessions() as s:
                row = (await s.execute(select(WebhookDeliveryRow))).scalar_one()
            assert (row.status, row.attempts, row.last_status_code) == ("delivered", 3, 200)
            last = received[-1]
            assert webhooks.verify(SECRET, last.headers[webhooks.SIGNATURE_HEADER], last.content)
            assert last.headers[webhooks.DELIVERY_HEADER] == row.id
            payload = json.loads(last.content)
            assert set(payload) == {"event", "delivery_id", "case_id", "job_id", "outcome", "result_url"}

    asyncio.run(scenario())


def test_webhook_gives_up_after_max_attempts(db_url: str) -> None:
    async def scenario() -> None:
        clock = Clock()
        async with queue_for(db_url, clock=clock, webhook_url="https://hooks.example/decided") as queue:
            await submit(queue, DEV[0][0], "k")
            await WorkerPool(queue, agent(), concurrency=1).run_until_idle()
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
            sender = webhooks.WebhookSender(queue.sessions, SECRET, client, max_attempts=3, clock=clock)
            for _ in range(5):
                await sender.deliver_due()
                clock.advance(3600)
            await client.aclose()
            async with queue.sessions() as s:
                row = (await s.execute(select(WebhookDeliveryRow))).scalar_one()
            assert (row.status, row.attempts) == ("failed", 3)

    asyncio.run(scenario())
