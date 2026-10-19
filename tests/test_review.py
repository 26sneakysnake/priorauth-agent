"""Reviewer workflow: queue, a case with its citations located exactly, review actions, feedback to dev cases."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from eval import review_to_cases
from eval.run import load_split
from priorauth.agent.verifier import locate, normalize, normalize_quote
from priorauth.api.fhir import case_to_bundle
from priorauth.data.schema import Case, Gold, Trap
from priorauth.policy.models import Status
from tests.test_api import api, login, one_per_trap, wait_done
from tests.test_data import load

SPLITS = {s: load(s) for s in ("dev", "test", "robust")}


def test_every_verified_gold_quote_is_located_exactly() -> None:
    """The offsets the UI highlights cover exactly the characters the verifier matched."""
    located = 0
    for pairs in SPLITS.values():
        for case, gold in pairs:
            for ev in [e for gc in gold.criteria.values() for e in gc.evidence] + list(gold.injection_evidence):
                text = case.page(ev.page).text
                span = locate(text, ev.quote)
                assert span is not None, (case.case_id, ev.quote)
                start, end = span
                assert normalize(text[start:end]) == normalize_quote(ev.quote)
                assert not text[start].isspace() and not text[end - 1].isspace()
                located += 1
    assert located > 1000


def test_locate_maps_normalized_matches_back_to_the_original_characters() -> None:
    text = "Line one.\nPain 4/10   with\n\tactivity — since “March”.\n"
    start, end = locate(text, '"pain 4/10 with activity - since "march""') or (0, 0)
    assert text[start:end] == "Pain 4/10   with\n\tactivity — since “March"
    assert locate(text, "not in the page") is None
    assert locate(text, "...") is None


async def _review_setup(client: httpx.AsyncClient) -> dict[str, tuple[Case, Gold, str]]:
    """Submit one case per trap as the system user, wait for every decision; key -> (case, gold, case_id)."""
    system = {"Authorization": f"Bearer {await login(client, 'demo-system')}"}
    out = {}
    for case, gold in one_per_trap():
        r = await client.post(
            "/cases", json=case_to_bundle(case, case.case_id), headers=system | {"Idempotency-Key": case.case_id}
        )
        out[case.case_id] = (case, gold, r.json()["case_id"])
    for _, _, case_id in out.values():
        await wait_done(client, case_id, headers=system)
    return out


def test_queue_lists_pended_cases_with_reasons_and_age(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, role="reviewer") as (client, _):
            cases = await _review_setup(client)
            queue = (await client.get("/review/queue")).json()
            expected = {cid for _, gold, cid in cases.values() if gold.expected_decision == "PEND"}
            assert {item["case_id"] for item in queue} == expected
            for item in queue:
                assert item["reasons"] and item["age_seconds"] >= 0 and item["review_status"] is None
                assert all(r["code"] for r in item["reasons"])
            procedure = queue[0]["procedure_code"]
            filtered = (await client.get("/review/queue", params={"procedure": procedure})).json()
            assert filtered and {i["procedure_code"] for i in filtered} == {procedure}

    asyncio.run(scenario())


def test_case_review_shows_original_pages_and_located_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, role="reviewer") as (client, _):
            cases = await _review_setup(client)
            seen_evidence = 0
            for case, _gold, case_id in cases.values():
                view = (await client.get(f"/cases/{case_id}/review")).json()
                assert [p["text"] for p in view["pages"]] == [p.text for p in case.pages]  # not redacted for people
                assert view["extracted_by"] == "agent_fake+protected"
                assert len(view["criteria"]) >= 4
                for criterion in view["criteria"]:
                    for ev in criterion["evidence"]:
                        text = view["pages"][ev["page"] - 1]["text"]
                        assert ev["valid"] and ev["start"] is not None
                        assert normalize(text[ev["start"] : ev["end"]]) == normalize_quote(ev["quote"])
                        seen_evidence += 1
            assert seen_evidence > 10
            assert (await client.get("/cases/00000000-0000-4000-8000-000000000000/review")).status_code == 404

    asyncio.run(scenario())


def test_review_actions_validate_and_update_the_queue(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with api(tmp_path, role="reviewer") as (client, _):
            cases = await _review_setup(client)
            pended = [(c, g, cid) for c, g, cid in cases.values() if g.expected_decision == "PEND"]
            approved = next(cid for _, g, cid in cases.values() if g.expected_decision == "APPROVE")
            (_, _, a), (case_b, gold_b, b), (_, _, c) = pended[:3]

            async def review(case_id: str, **body: Any) -> httpx.Response:
                return await client.post(f"/cases/{case_id}/reviews", json=body)

            assert (await review(a, action="request_info")).status_code == 422  # a reason is required
            assert (await review(a, action="request_info", reason="  ")).status_code == 422
            assert (await review(a, action="flag_error", criterion_id="x")).status_code == 422
            assert (await review(a, action="approve", criterion_id="x")).status_code == 422
            assert (await review(a, action="nonsense")).status_code == 422
            bad_criterion = await review(
                a, action="flag_error", criterion_id="NOT.a_criterion", corrected_status="MET", comment="c"
            )
            assert bad_criterion.status_code == 422
            assert (await review(approved, action="approve")).status_code == 409  # the engine already approved it
            assert (await review("00000000-0000-4000-8000-000000000000", action="approve")).status_code == 404

            r = await review(a, action="request_info", reason="Need the latest therapy note.")
            assert r.status_code == 201 and r.json()["comment"] == "Need the latest therapy note."
            target = gold_b.target_criterion or next(iter(gold_b.criteria))
            r = await review(b, action="flag_error", criterion_id=target, corrected_status="MET", comment="Wrong page")
            assert r.status_code == 201 and r.json()["corrected_status"] == "MET"
            assert (await review(c, action="approve")).status_code == 201

            queue = {i["case_id"]: i for i in (await client.get("/review/queue")).json()}
            assert c not in queue  # approved by a person: off the queue
            assert queue[a]["review_status"] == "request_info" and queue[b]["review_status"] == "flag_error"
            view = (await client.get(f"/cases/{b}/review")).json()
            assert [(rv["action"], rv["criterion_id"]) for rv in view["reviews"]] == [("flag_error", target)]

            system = {"Authorization": f"Bearer {await login(client, 'demo-system')}"}
            assert (
                await client.post(f"/cases/{a}/reviews", json={"action": "approve"}, headers=system)
            ).status_code == 403
            admin = {"Authorization": f"Bearer {await login(client, 'demo-admin')}"}
            actions = [e["action"] for e in (await client.get("/audit", headers=admin, params={"limit": 1000})).json()]
            for expected in (
                "review.queue",
                "review.read",
                "review.request_info",
                "review.flag_error",
                "review.approve",
            ):
                assert expected in actions

        # The feedback file: one line per review, identifiers and labels only, never the comment.
        lines = [json.loads(x) for x in (tmp_path / "cases_from_review.jsonl").read_text().splitlines()]
        assert [x["action"] for x in lines] == ["request_info", "flag_error", "approve"]
        flagged = lines[1]
        assert (flagged["case_id"], flagged["criterion_id"], flagged["corrected_status"]) == (b, target, "MET")
        assert lines[2]["expected_decision"] == "APPROVE"
        raw = (tmp_path / "cases_from_review.jsonl").read_text()
        assert "Wrong page" not in raw and "therapy note" not in raw and case_b.patient.name not in raw
        # Comments are encrypted at rest.
        con = sqlite3.connect(tmp_path / "api.db")
        stored = [row[0] for row in con.execute("SELECT comment FROM reviews ORDER BY created_at")]
        con.close()
        assert len(stored) == 3 and stored[2] is None  # the approval had no comment
        assert all(s.startswith("gAAAAA") for s in stored[:2])

        # The feedback becomes dev evaluation cases with the reviewer's labels.
        report = await review_to_cases.convert(
            f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
            tmp_path / "cases_from_review.jsonl",
            tmp_path / "out" / "dev_review",
        )
        assert report == {"reviewed_cases": 3, "written": 2, "skipped_no_label": 1, "not_in_database": 0}
        pairs = {gold.case_id: (case, gold) for case, gold in load_split(tmp_path / "out", "dev_review")}
        flagged_case, flagged_gold = pairs[f"review-{b}"]
        assert flagged_gold.trap is Trap.FROM_REVIEW and flagged_gold.criteria[target].status is Status.MET
        assert flagged_gold.target_criterion == target and "corrected" in flagged_gold.criteria[target].rationale
        assert [p.text for p in flagged_case.pages] == [p.text for p in case_b.pages]
        approved_gold = pairs[f"review-{c}"][1]
        assert all(gc.status is Status.MET for gc in approved_gold.criteria.values())

    asyncio.run(scenario())
