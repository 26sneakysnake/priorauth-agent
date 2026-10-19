"""Smoke test of a running demo: what a stranger would do in two minutes, checked.

    python -m priorauth.demo.smoke --api https://<api> [--ui https://<ui>]       # make smoke-demo URL=... UI_URL=...

1. API: /healthz answers, /docs (interactive OpenAPI) is served, the schema lists the demo routes.
2. /demo/info gives the public reviewer account and says the data is synthetic; sign in with it.
3. At least 8 examples; run the clean record and the hidden-instruction record through the queue.
4. The clean record is APPROVED with at least one located citation; the hidden-instruction record is PENDED with
   INJECTION_DETECTED, and is never approved.
With --ui, steps 2 to 4 go through the UI's /api proxy (the path a browser takes), and /login and /try must load.
Exit code 0 when every check passes, 1 otherwise (each failure is printed).
"""

import argparse
import asyncio
import sys
import time
from typing import Any

import httpx

CLEAN = "clean-approve-pt"
INJECTION = "injection"
TIMEOUT_S = 90.0


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


async def decided(client: httpx.AsyncClient, case_id: str, headers: dict[str, str]) -> dict[str, Any]:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        r = await client.get(f"/cases/{case_id}", headers=headers)
        check(r.status_code == 200, f"GET /cases/{case_id}: {r.status_code}")
        view: dict[str, Any] = r.json()
        if view["job"]["status"] in ("done", "failed"):
            return view
        await asyncio.sleep(0.5)
    raise SmokeFailure(f"case {case_id} not decided within {TIMEOUT_S} s")


async def check_api(api: httpx.AsyncClient) -> None:
    r = await api.get("/healthz")
    check(r.status_code == 200 and r.json() == {"status": "ok"}, f"/healthz: {r.status_code}")
    check((await api.get("/docs")).status_code == 200, "/docs is not served")
    schema = (await api.get("/openapi.json")).json()
    check("/demo/examples/{example_id}/run" in schema["paths"], "the demo routes are not in the schema")


async def check_flow(flow: httpx.AsyncClient, out: list[str]) -> None:
    r = await flow.get("/demo/info")
    check(r.status_code == 200, f"/demo/info: {r.status_code}")
    info = r.json()
    check("synthetic" in info["notice"].lower(), "the demo does not say its data is synthetic")
    r = await flow.post(
        "/auth/token", json={"username": info["reviewer_username"], "password": info["reviewer_password"]}
    )
    check(r.status_code == 200 and r.json()["role"] == "reviewer", f"sign-in with the demo account: {r.status_code}")
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    examples = {e["id"]: e for e in (await flow.get("/demo/examples")).json()}
    check(len(examples) >= 8, f"{len(examples)} examples, expected at least 8")
    check({CLEAN, INJECTION} <= set(examples), "the clean and hidden-instruction examples are missing")
    for example_id, expected in ((CLEAN, "APPROVE"), (INJECTION, "PEND")):
        r = await flow.post(f"/demo/examples/{example_id}/run", headers=headers)
        check(r.status_code == 202, f"run {example_id}: {r.status_code} {r.text[:200]}")
        case_id = r.json()["case_id"]
        view = await decided(flow, case_id, headers)
        if example_id == INJECTION:
            check(view["outcome"] != "APPROVE", "the hidden-instruction record was APPROVED")
        check(view["outcome"] == expected, f"{example_id}: {view['outcome']}, expected {expected}")
        review = (await flow.get(f"/cases/{case_id}/review", headers=headers)).json()
        if example_id == CLEAN:
            located = [e for c in review["criteria"] for e in c["evidence"] if e["start"] is not None]
            check(len(located) > 0, "no citation located on the clean record")
        else:
            codes = [reason["code"] for reason in review["reasons"]]
            check("INJECTION_DETECTED" in codes, f"hidden instruction not flagged: {codes}")
        out.append(f"{example_id}: {view['outcome']} (expected {expected})")


async def check_ui(ui: httpx.AsyncClient) -> None:
    for path in ("/login", "/try"):
        r = await ui.get(path)
        check(r.status_code == 200, f"UI {path}: {r.status_code}")


async def smoke(api: httpx.AsyncClient, ui: httpx.AsyncClient | None = None) -> list[str]:
    out: list[str] = []
    await check_api(api)
    out.append("API: /healthz, /docs and the demo routes are served")
    if ui is not None:
        await check_ui(ui)
        out.append("UI: /login and /try load")
        async with httpx.AsyncClient(
            base_url=str(ui.base_url).rstrip("/") + "/api", transport=ui._transport, timeout=30
        ) as through_ui:
            await check_flow(through_ui, out)
    else:
        await check_flow(api, out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", required=True, help="the API base URL")
    parser.add_argument("--ui", help="the UI base URL (the flow then goes through its /api proxy)")
    args = parser.parse_args(argv)

    async def run() -> list[str]:
        async with httpx.AsyncClient(base_url=args.api, timeout=30, follow_redirects=True) as api:
            if not args.ui:
                return await smoke(api)
            async with httpx.AsyncClient(base_url=args.ui, timeout=30, follow_redirects=True) as ui:
                return await smoke(api, ui)

    try:
        for line in asyncio.run(run()):
            print(f"ok  {line}")
    except (SmokeFailure, httpx.HTTPError) as e:
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
