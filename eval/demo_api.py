"""Demo API for the reviewer UI, with a SIMULATED model: the real app, its auth, encryption and audit, but the model is
the scripted oracle of priorauth.llm.fake (it replays gold labels; it is not a model and measures nothing about one).

    uv run python -m eval.demo_api --port 8000 --per-trap 2

On start it seeds synthetic dev cases through POST /cases as demo-system and waits for their decisions, so the
review queue has pended cases with reasons and located evidence. Every case says `extracted_by:
scripted-oracle-not-a-model+protected` in its review view. Defaults: a fresh SQLite file in a temporary directory, an
ephemeral field key, random JWT and audit secrets; demo users demo-system, demo-reviewer and demo-admin with the
password from PRIORAUTH_DEMO_PASSWORD (a random one is printed if it is unset).
"""

import argparse
import asyncio
import os
import secrets
import tempfile
from pathlib import Path

import httpx
import uvicorn

from eval.run import load_split
from priorauth.agent.loop import AgentSystem
from priorauth.api.app import Settings, create_app
from priorauth.api.fhir import case_to_bundle
from priorauth.api.review import DEFAULT_FEEDBACK_PATH
from priorauth.data.schema import Case, Gold, Trap
from priorauth.llm.fake import fake_client, protected_oracle
from priorauth.llm.pricing import HAIKU
from priorauth.pipeline import System
from priorauth.security.crypto import ENV as FIELD_KEY_ENV
from priorauth.security.crypto import new_key
from priorauth.security.model_view import ProtectedSystem

SYSTEM_NAME = "scripted-oracle-not-a-model"


def seed_cases(data: Path, per_trap: int) -> list[tuple[Case, Gold]]:
    picked: dict[Trap, list[tuple[Case, Gold]]] = {}
    for case, gold in load_split(data, "dev"):
        bucket = picked.setdefault(gold.trap, [])
        if len(bucket) < per_trap:
            bucket.append((case, gold))
    return [pair for bucket in picked.values() for pair in bucket]


async def serve(port: int, per_trap: int, data: Path, database_url: str | None, feedback: str | None) -> None:
    pairs = seed_cases(data, per_trap)
    oracle = protected_oracle(load_split(data, "dev"))

    def system() -> System:
        return ProtectedSystem(AgentSystem(SYSTEM_NAME, HAIKU, "agent@v2", 0.10, llm=fake_client(oracle)))

    os.environ.setdefault(FIELD_KEY_ENV, new_key())
    password = os.environ.get("PRIORAUTH_DEMO_PASSWORD") or secrets.token_urlsafe(12)
    if not os.environ.get("PRIORAUTH_DEMO_PASSWORD"):
        print(f"demo password (demo-reviewer, demo-admin, demo-system): {password}", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            database_url=database_url or f"sqlite+aiosqlite:///{Path(tmp) / 'demo.db'}",
            jwt_secret=secrets.token_urlsafe(32),
            audit_key=secrets.token_urlsafe(32),
            demo_password=password,
            review_feedback_path=feedback,
        )
        app = create_app(settings, system_factory=system)
        async with app.router.lifespan_context(app):  # seed in-process first: the port opens once cases are decided
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
                r = await client.post("/auth/token", json={"username": "demo-system", "password": password})
                headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
                ids = []
                for case, _ in pairs:
                    r = await client.post(
                        "/cases",
                        json=case_to_bundle(case, case.case_id),
                        headers=headers | {"Idempotency-Key": case.case_id},
                    )
                    ids.append(r.json()["case_id"])
                for case_id in ids:
                    while (await client.get(f"/cases/{case_id}", headers=headers)).json()["job"]["status"] not in (
                        "done",
                        "failed",
                    ):
                        await asyncio.sleep(0.05)
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
            )
            print(f"demo API on http://127.0.0.1:{port}: {len(ids)} synthetic cases (simulated model)", flush=True)
            await server.serve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--per-trap", type=int, default=2)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--feedback",
        default=os.environ.get("PRIORAUTH_REVIEW_FEEDBACK_PATH", DEFAULT_FEEDBACK_PATH),
        help="review feedback file (one JSON line per review); '' to disable",
    )
    args = parser.parse_args(argv)
    asyncio.run(serve(args.port, args.per_trap, args.data, args.database_url, args.feedback or None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
