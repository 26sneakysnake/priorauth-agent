"""Asyncio worker pool: claims jobs, runs the decision pipeline, stores results.

At most `concurrency` jobs run at once in one pool (an asyncio.Semaphore). While a job runs, a heartbeat renews its
lease. The pipeline itself never raises (a system error pends the case, see priorauth.pipeline); an exception here
is an infrastructure error, and the job is released for another attempt.
"""

import asyncio
import logging
import os
import random
import socket
import uuid
from contextlib import suppress
from dataclasses import dataclass

from priorauth.data.schema import Case
from priorauth.jobs.queue import ClaimedJob, JobQueue, spent_today
from priorauth.obs.tracing import set_attrs, span
from priorauth.pipeline import System, run_case

log = logging.getLogger("priorauth.worker")

# Error kinds of a pipeline run that mean "the model could not be reached", not "the case is unclear": the job is
# retried later instead of being pended (up to max_attempts; the last attempt's PEND is then kept).
TRANSIENT_KINDS = frozenset({"CircuitOpen", "RetriesExhausted"})


@dataclass(frozen=True)
class DailyBudget:
    """Stop claiming new jobs once today's LLM spend, plus a reservation for the jobs in flight, reaches the cap.
    Queued jobs simply wait (for the next UTC day or a higher cap): nothing is dropped or decided."""

    cap_usd: float
    reserve_per_job_usd: float  # the per-case cap of the system: the most an in-flight job can still spend

    async def allows(self, queue: JobQueue, in_flight: int) -> bool:
        spent = await spent_today(queue.sessions, queue.clock())
        return spent + in_flight * self.reserve_per_job_usd < self.cap_usd


def new_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class WorkerPool:
    def __init__(
        self,
        queue: JobQueue,
        system: System,
        *,
        concurrency: int = 4,
        poll_s: float = 0.2,
        heartbeat_s: float | None = None,
        worker_id: str | None = None,
        budget: DailyBudget | None = None,
        retry_delay_s: float = 30.0,
    ) -> None:
        self.queue = queue
        self.system = system
        self.budget = budget
        self.retry_delay_s = retry_delay_s
        self.paused_for_budget = False
        self.rng = random.Random()
        self.concurrency = concurrency
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else queue.lease.total_seconds() / 3
        self.worker_id = worker_id or new_worker_id()
        self.tasks: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        sem = asyncio.Semaphore(self.concurrency)
        while not stop.is_set():
            await sem.acquire()
            job = await self.queue.claim(self.worker_id) if await self._budget_allows() else None
            if job is None:
                sem.release()
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
                continue
            task = asyncio.create_task(self._process(job, sem))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def run_until_idle(self) -> int:
        """Process jobs until none is claimable (tests and batch use); returns the number processed."""
        done = 0
        sem = asyncio.Semaphore(self.concurrency)
        while True:
            batch: list[asyncio.Task[None]] = []
            for _ in range(self.concurrency):
                job = await self.queue.claim(self.worker_id) if await self._budget_allows(len(batch)) else None
                if job is None:
                    break
                await sem.acquire()
                batch.append(asyncio.create_task(self._process(job, sem)))
            if not batch:
                return done
            await asyncio.gather(*batch)
            done += len(batch)

    async def _budget_allows(self, extra_in_flight: int = 0) -> bool:
        if self.budget is None:
            return True
        allowed = await self.budget.allows(self.queue, len(self.tasks) + extra_in_flight)
        if allowed != (not self.paused_for_budget):
            self.paused_for_budget = not allowed
            log.warning("daily cost cap reached: not claiming" if not allowed else "daily cost cap: resuming")
        return allowed

    async def _heartbeat(self, job: ClaimedJob) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            if not await self.queue.heartbeat(job.job_id, self.worker_id):
                log.warning("lease lost", extra={"job_id": job.job_id})
                return

    async def _process(self, job: ClaimedJob, sem: asyncio.Semaphore) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            with span("job.process", job__id=job.job_id, case__id=job.case_id, job__attempt=job.attempts) as s:
                case = Case.model_validate_json(job.case_json)
                run = await run_case(self.system, case)
                kind = run.error.split(":", 1)[0] if run.error else None
                set_attrs(
                    s,
                    procedure__code=case.procedure_code,
                    job__outcome=run.decision.outcome.value,
                    job__error_kind=kind,
                    llm__calls=run.usage.llm_calls,
                    tokens_in=run.usage.input_tokens + run.usage.cache_read_tokens + run.usage.cache_write_tokens,
                    tokens_out=run.usage.output_tokens,
                    cost_usd=run.usage.cost_usd,
                )
                if kind in TRANSIENT_KINDS and job.attempts < self.queue.max_attempts:
                    delay = self.retry_delay_s * 2 ** (job.attempts - 1) * self.rng.uniform(0.5, 1.0)
                    await self.queue.release(job.job_id, self.worker_id, kind or "transient", delay_s=delay)
                    log.warning("job requeued", extra={"job_id": job.job_id, "error_kind": kind})
                    return
                stored = await self.queue.complete(job.job_id, self.worker_id, run, system_name=self.system.name)
            log.info(
                "job finished",
                extra={
                    "job_id": job.job_id,
                    "attempt": job.attempts,
                    "outcome": run.decision.outcome.value,
                    "stored": stored,
                    "cost_usd": run.usage.cost_usd,
                },
            )
        except Exception as e:  # infrastructure error: give the job back
            log.error("job error", extra={"job_id": job.job_id, "error_kind": type(e).__name__})
            await self.queue.release(job.job_id, self.worker_id, type(e).__name__, delay_s=self.retry_delay_s)
        finally:
            heartbeat.cancel()
            sem.release()
