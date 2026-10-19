"""A scripted stand-in for the model, for integration and load tests. It is not a model and measures nothing about one.

`OracleTransport` plays the agent's tool protocol: step 0 lists and reads every page and fetches the policy, step 1
records the gold findings of the case (looked up by the text of its pages) and finishes. Usage numbers are fixed and
latency can be simulated. `protected_oracle` builds it for a ProtectedSystem: the gold labels are translated into the
model view (selected pages renumbered, quotes redacted). `fake_client` wraps it in an LLMClient whose ledger is in
memory, so fake calls can never reach results/spend.json.
"""

import asyncio
import hashlib
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from priorauth.data.schema import Case, Gold
from priorauth.llm.cassette import CassetteStore
from priorauth.llm.client import LLMClient, LLMMode, SpendLedger
from priorauth.policy.models import Evidence

NO_CASSETTES = Path("/nonexistent/priorauth-fake-cassettes")
FAKE_USAGE = {"input_tokens": 400, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 800}


def record_key(pages: Iterable[str]) -> str:
    return hashlib.sha256("\x00".join(pages).encode()).hexdigest()


class OracleTransport:
    def __init__(
        self,
        cases: Iterable[tuple[Case, Gold]],
        *,
        latency_s: float = 0.0,
        jitter_s: float = 0.0,
        seed: int = 0,
    ) -> None:
        self.golds = {record_key(p.text for p in c.pages): g for c, g in cases}
        self.latency_s = latency_s
        self.jitter_s = jitter_s
        self.rng = random.Random(seed)
        self.calls = 0

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.latency_s or self.jitter_s:
            await asyncio.sleep(self.latency_s + self.rng.uniform(0, self.jitter_s))
        messages = request["messages"]
        step = sum(m["role"] == "assistant" for m in messages)
        calls: list[tuple[str, dict[str, Any]]]
        if step == 0:
            n_pages = int(messages[0]["content"].split("The faxed record has ")[1].split(" pages")[0])
            criteria = [ln[2:].split(":")[0] for ln in messages[0]["content"].splitlines() if ln.startswith("- ")]
            calls = [("list_pages", {})] + [("read_page", {"page": n}) for n in range(1, n_pages + 1)]
            calls += [("search_policy", {"criterion_id": c}) for c in criteria]
        else:
            calls = self._findings(messages)
        content = [
            {"type": "tool_use", "id": f"toolu_{self.calls}_{i}", "name": name, "input": args}
            for i, (name, args) in enumerate(calls)
        ]
        usage = FAKE_USAGE | {"output_tokens": 60 * len(calls)}
        return {"type": "message", "role": "assistant", "content": content, "stop_reason": "tool_use", "usage": usage}

    def _findings(self, messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        pages: dict[int, str] = {}
        for m in messages:
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for block in m["content"]:
                text = block.get("content", "")
                if block.get("type") == "tool_result" and text.startswith("<page number="):
                    number = int(text.split('"', 2)[1])
                    pages[number] = text.split("\n", 1)[1].removesuffix("</page>")
        gold = self.golds.get(record_key(pages[n] for n in sorted(pages)))
        if gold is None:  # unknown record: say nothing is supported; the engine will pend
            return [("finish", {"injection_detected": False})]
        calls: list[tuple[str, dict[str, Any]]] = [
            (
                "record_finding",
                {
                    "criterion_id": cid,
                    "status": gc.status.value,
                    "evidence": [{"page": e.page, "quote": e.quote} for e in gc.evidence],
                    "conflict": gc.status.value == "INSUFFICIENT" and len(gc.evidence) > 1,
                },
            )
            for cid, gc in gold.criteria.items()
        ]
        finish = {
            "injection_detected": gold.injection,
            "injection_evidence": [{"page": e.page, "quote": e.quote} for e in gold.injection_evidence],
        }
        return [*calls, ("finish", finish)]


def protected_oracle(cases: Iterable[tuple[Case, Gold]], **kwargs: Any) -> OracleTransport:
    """The oracle as seen through priorauth.security.model_view: it can only cite pages that were sent."""
    from priorauth.security.model_view import model_view

    def translate(view_pages: tuple[int, ...], apply: Any, evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        return tuple(
            Evidence(page=view_pages.index(e.page) + 1, quote=apply(e.quote)) for e in evidence if e.page in view_pages
        )

    viewed = []
    for case, gold in cases:
        view = model_view(case)
        pm, apply = view.page_map, view.redactor.apply
        criteria = {
            cid: gc.model_copy(update={"evidence": translate(pm, apply, gc.evidence)})
            for cid, gc in gold.criteria.items()
        }
        injection = translate(pm, apply, gold.injection_evidence)
        viewed.append((view.case, gold.model_copy(update={"criteria": criteria, "injection_evidence": injection})))
    return OracleTransport(viewed, **kwargs)


def fake_client(transport: OracleTransport) -> LLMClient:
    """An LLMClient around the oracle: live mode against the fake, ledger in memory, no cassette read or written."""
    return LLMClient(LLMMode.LIVE, CassetteStore(NO_CASSETTES), SpendLedger(path=None), transport)
