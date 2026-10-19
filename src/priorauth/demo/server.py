"""The demo server: the API (workers in-process) on a fresh SQLite database, seeded with the example cases.

    python -m priorauth.demo --host 0.0.0.0 --port 8000      # what the Docker image runs

Every model response comes from eval/cassettes/agent_v2_haiku_pipeline/ (real claude-haiku-5-5 responses recorded
by eval/pipeline.py), through a replay-only client: no network call, no key. An example without a recording stops
the server at start instead of being decided by anything else. State lives in a temporary directory and is lost on
restart, by design. Accounts: demo-reviewer, whose password is public (shown on the login page and by /demo/info);
demo-system, with a random password nobody sees (it submits the examples); no admin account.

Environment: PRIORAUTH_DEMO_REVIEWER_PASSWORD (default below), PRIORAUTH_DEMO_DATA (default data),
PRIORAUTH_DEMO_CASSETTES (default eval/cassettes), PRIORAUTH_CORS_ORIGINS, DEMO_LIVE_* (priorauth.demo.live).
"""

import asyncio
import os
import secrets
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import select

from priorauth.agent.loop import AgentSystem
from priorauth.api.app import Settings, create_app
from priorauth.api.fhir import case_to_bundle, parse_bundle
from priorauth.demo.examples import load_examples
from priorauth.demo.live import LiveConfig, LiveRunner
from priorauth.demo.routes import DemoState, router
from priorauth.jobs.db import JobRow
from priorauth.jobs.queue import JobQueue, body_digest
from priorauth.llm.cassette import CassetteStore
from priorauth.llm.client import LLMClient, LLMMode, Transport
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import System
from priorauth.security.auth import Users
from priorauth.security.crypto import ENV as FIELD_KEY_ENV
from priorauth.security.crypto import new_key
from priorauth.security.model_view import ProtectedSystem

SYSTEM_NAME = "agent_v2_haiku+protected (replayed claude-haiku-5-5 responses)"
REVIEWER = "demo-reviewer"
DEFAULT_REVIEWER_PASSWORD = "synthetic-demo-only"  # noqa: S105  # public on purpose: shown on the login page


class DemoStartError(RuntimeError):
    pass


@dataclass
class DemoConfig:
    data_dir: Path = Path("data")
    cassettes_dir: Path = Path("eval/cassettes")
    reviewer_password: str = DEFAULT_REVIEWER_PASSWORD
    database_url: str | None = None  # default: a SQLite file in a temporary directory
    live: LiveConfig = field(default_factory=LiveConfig)
    live_transport: Callable[[], Transport] | None = None  # tests only
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "DemoConfig":
        env = os.environ.get
        return cls(
            data_dir=Path(env("PRIORAUTH_DEMO_DATA", "data")),
            cassettes_dir=Path(env("PRIORAUTH_DEMO_CASSETTES", "eval/cassettes")),
            reviewer_password=env("PRIORAUTH_DEMO_REVIEWER_PASSWORD") or DEFAULT_REVIEWER_PASSWORD,
            live=LiveConfig.from_env(),
            cors_origins=tuple(o.strip() for o in env("PRIORAUTH_CORS_ORIGINS", "").split(",") if o.strip()),
        )


async def _wait_decided(app: FastAPI, job_ids: list[str]) -> list[JobRow]:
    while True:
        async with app.state.sessions() as s:
            jobs = list((await s.execute(select(JobRow).where(JobRow.id.in_(job_ids)))).scalars())
        if all(j.status in ("done", "failed") for j in jobs):
            return jobs
        await asyncio.sleep(0.05)


def replay_system(cassettes_dir: Path) -> Callable[[], System]:
    """The production system (agent v2 Haiku behind the model view), answering only from recorded responses."""
    replay = LLMClient(LLMMode.REPLAY, CassetteStore(cassettes_dir))  # never the network

    def system() -> System:
        return ProtectedSystem(AgentSystem("agent_v2_haiku", HAIKU, "agent@v2", 0.10, llm=replay), name=SYSTEM_NAME)

    return system


@asynccontextmanager
async def demo_app(config: DemoConfig) -> AsyncIterator[FastAPI]:
    """The demo app with its lifespan entered, users created and examples decided."""
    os.environ.setdefault(FIELD_KEY_ENV, new_key())
    examples = load_examples(config.data_dir)
    system = replay_system(config.cassettes_dir)
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            database_url=config.database_url or f"sqlite+aiosqlite:///{Path(tmp) / 'demo.db'}",
            workers=4,
            jwt_secret=secrets.token_urlsafe(32),
            audit_key=secrets.token_urlsafe(32),
            demo_password=None,
            review_feedback_path=None,
            cors_origins=config.cors_origins,
        )
        app = create_app(settings, system_factory=system)
        app.include_router(router)
        async with app.router.lifespan_context(app):
            users: Users = app.state.users
            await users.ensure(REVIEWER, config.reviewer_password, "reviewer")
            submitter = await users.ensure("demo-system", secrets.token_urlsafe(24), "system")
            queue: JobQueue = app.state.queue
            seeded: dict[str, str] = {}
            job_ids = []
            for example, case, _ in examples:
                bundle = case_to_bundle(case, case.case_id)
                parsed = parse_bundle(bundle, case_id=f"{example.id}-seed")
                sub = await queue.submit(parsed, f"seed-{example.id}", body_digest(example.id.encode()), submitter.id)
                seeded[example.id] = sub.case_id
                job_ids.append(sub.job_id)
            jobs = await _wait_decided(app, job_ids)
            missing = [j.case_id for j in jobs if j.error == "CassetteMiss"]
            if missing:
                raise DemoStartError(f"no recorded model response for {missing}; run eval/pipeline.py on dev")
            app.state.demo = DemoState(
                examples=examples,
                system_user_id=submitter.id,
                reviewer_username=REVIEWER,
                reviewer_password=config.reviewer_password,
                system_name=SYSTEM_NAME,
                live=LiveRunner(config.live, transport_factory=config.live_transport),
                seeded=seeded,
            )
            yield app


async def serve(config: DemoConfig, host: str, port: int) -> None:
    import uvicorn

    async with demo_app(config) as app:  # the port opens once every example is decided
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off", proxy_headers=True)
        )
        print(f"demo API on http://{host}:{port} (examples decided from recorded responses)", flush=True)
        await server.serve()
