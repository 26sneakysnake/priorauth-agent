"""Cassettes: recorded model calls, replayed byte for byte in CI without an API key.

Each recorded call is one JSON line in eval/cassettes/<scope>.jsonl (scope = "<system>/<split>"). The lookup key is
the sha256 of the request as sent (model, system, tools, messages, limits), serialized canonically; it depends on
nothing else. The request itself is not stored (it is rebuilt from code + data on replay), and neither is any
header: an API key cannot end up in a cassette. Latency and cost are stored at record time so that replayed results
are deterministic.
"""

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from priorauth.llm.pricing import TokenUsage

DEFAULT_ROOT = Path("eval/cassettes")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def request_key(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(request).encode()).hexdigest()


class Interaction(BaseModel):
    """One recorded request/response pair. `label` and `step` only help a human find a call; they are not in the key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    step: int
    model: str
    response: dict[str, Any]  # the Messages API response body (content, stop_reason, usage, ...)
    usage: TokenUsage
    cost_usd: float
    latency_ms: float


class CassetteStore:
    """All cassettes under `root`, indexed by key. New interactions are appended to their scope file immediately, so
    an interrupted recording keeps what was paid for."""

    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = root
        self._by_key: dict[str, Interaction] = {}
        self._touched: set[str] = set()
        self.used: set[str] = set()
        for path in sorted(root.rglob("*.jsonl")) if root.exists() else []:
            for interaction in read_file(path):
                self._by_key.setdefault(interaction.key, interaction)

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, key: str) -> Interaction | None:
        found = self._by_key.get(key)
        if found is not None:
            self.used.add(key)
        return found

    def put(self, scope: str, interaction: Interaction) -> None:
        path = self.path(scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(canonical_json(interaction.model_dump(mode="json")) + "\n")
        self._by_key.setdefault(interaction.key, interaction)
        self.used.add(interaction.key)
        self._touched.add(scope)

    def path(self, scope: str) -> Path:
        if not scope or scope.startswith("/") or ".." in scope.split("/"):
            raise ValueError(f"invalid cassette scope {scope!r}")
        return self.root / f"{scope}.jsonl"

    def scope_interactions(self, prefix: str) -> list[Interaction]:
        """Every interaction recorded under eval/cassettes/<prefix>... (for cost estimates and audits)."""
        base = self.root / prefix
        paths = sorted(base.rglob("*.jsonl")) if base.is_dir() else []
        return [i for path in paths for i in read_file(path)]

    def compact(self) -> None:
        """Rewrite every file written during this session: one line per key, sorted by (label, step, key)."""
        for scope in sorted(self._touched):
            path = self.path(scope)
            unique = {i.key: i for i in read_file(path)}
            ordered = sorted(unique.values(), key=lambda i: (i.label, i.step, i.key))
            path.write_text("".join(canonical_json(i.model_dump(mode="json")) + "\n" for i in ordered), "utf-8")
        self._touched.clear()


def read_file(path: Path) -> Iterator[Interaction]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield Interaction.model_validate_json(line)
