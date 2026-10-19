"""Asyncio worker pool: claims jobs, runs the decision pipeline, stores results.

At most `concurrency` jobs run at once in one pool (an asyncio.Semaphore). While a job runs, a heartbeat renews its
lease. The pipeline itself never raises (a system error pends the case, see priorauth.pipeline); an exception here
is an infrastructure error, and the job is released for another attempt.
"""

import asyncio
import logging
import os
import socket
import uuid
from contextlib import suppress

from priorauth.data.schema import Case
from priorauth.jobs.queue import ClaimedJob, JobQueue
from priorauth.pipeline import System, run_case

log = logging.getLogger("priorauth.worker")


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
    ) -> None:
        self.queue = queue
        self.system = system
        self.concurrency = concurrency
        self.poll_s = poll_s
        self.heartbeat_s = heartbeat_s if heartbeat_s is not None else queue.lease.total_seconds() / 3
        self.worker_id = worker_id or new_worker_id()
        self.tasks: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        sem = asyncio.Semaphore(self.concurrency)
        while not stop.is_set():
            await sem.acquire()
            job = await self.queue.claim(self.worker_id)
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
            batch = []
            for _ in range(self.concurrency):
                job = await self.queue.claim(self.worker_id)
                if job is None:
                    break
                await sem.acquire()
                batch.append(asyncio.create_task(self._process(job, sem)))
            if not batch:
                return done
            await asyncio.gather(*batch)
            done += len(batch)

    async def _heartbeat(self, job: ClaimedJob) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            if not await self.queue.heartbeat(job.job_id, self.worker_id):
                log.warning("lease lost", extra={"job_id": job.job_id})
                return

    async def _process(self, job: ClaimedJob, sem: asyncio.Semaphore) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            case = Case.model_validate_json(job.case_json)
            run = await run_case(self.system, case)
            stored = await self.queue.complete(job.job_id, self.worker_id, run)
            log.info(
                "job finished",
                extra={
                    "job_id": job.job_id,
                    "attempt": job.attempts,
                    "outcome": run.decision.outcome.value,
                    "stored": stored,
                },
            )
        except Exception as e:  # infrastructure error: give the job back
            log.error("job error", extra={"job_id": job.job_id, "error_kind": type(e).__name__})
            await self.queue.release(job.job_id, self.worker_id, type(e).__name__)
        finally:
            heartbeat.cancel()
            sem.release()
