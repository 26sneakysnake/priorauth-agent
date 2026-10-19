"""LLM client: cassettes, spend ledger and cost caps, on a fake transport (no network, no key)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from priorauth.llm import client as llm
from priorauth.llm.cassette import CassetteStore, request_key
from priorauth.llm.pricing import HAIKU, SONNET, TokenUsage, cost_usd, max_cost_usd

REQUEST: dict[str, Any] = {
    "model": HAIKU,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Reply OK."}],
}


class FakeTransport:
    def __init__(self, usage: dict[str, int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 100}

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(request)
        return {
            "id": f"msg_{len(self.calls)}",
            "type": "message",
            "role": "assistant",
            "model": request["model"],
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "usage": self.usage,
        }


def make(tmp_path: Path, mode: llm.LLMMode, transport: FakeTransport | None = None) -> llm.LLMClient:
    store = CassetteStore(tmp_path / "cassettes")
    if mode is llm.LLMMode.REPLAY:
        return llm.LLMClient(mode, store)
    return llm.LLMClient(mode, store, llm.SpendLedger(tmp_path / "spend.json"), transport or FakeTransport())


def call(client: llm.LLMClient, request: dict[str, Any] = REQUEST, cap: float = 1.0) -> llm.LLMCall:
    return asyncio.run(client.create(request, meter=llm.CaseMeter(cap), label="dev-001", step=0))


def test_prices_follow_the_published_table() -> None:
    usage = TokenUsage(input_tokens=10_000, cache_write_tokens=10_000, cache_read_tokens=10_000, output_tokens=10_000)
    assert cost_usd(HAIKU, usage) == pytest.approx(10_000 * (0.10 + 0.125 + 0.01 + 0.50) / 1e6)
    assert cost_usd(SONNET, TokenUsage(output_tokens=1_000_000)) == pytest.approx(10.0)
    # Haiku 5.5 bills the whole request at the higher rates once the prompt exceeds 100,000 tokens.
    assert cost_usd(HAIKU, TokenUsage(input_tokens=100_001)) == pytest.approx(100_001 * 0.50 / 1e6)
    assert max_cost_usd(HAIKU, 1000, 100) == pytest.approx((1000 * 0.125 + 100 * 0.50) / 1e6)


def test_unpriced_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="no price"):
        call(make(tmp_path, llm.LLMMode.RECORD), {**REQUEST, "model": "claude-unknown"})


def test_record_then_replay_returns_the_same_call_without_network(tmp_path: Path) -> None:
    transport = FakeTransport()
    recorder = make(tmp_path, llm.LLMMode.RECORD, transport)
    recorder.begin_run("agent/dev", cap_usd=1.0)
    first = call(recorder)
    recorder.end_run()
    assert not first.replayed and len(transport.calls) == 1

    # Recording the same request again is free: it is served from the cassette.
    again = call(make(tmp_path, llm.LLMMode.RECORD, transport))
    assert again.replayed and len(transport.calls) == 1

    replayed = call(make(tmp_path, llm.LLMMode.REPLAY))
    assert replayed.replayed
    assert (replayed.response, replayed.usage, replayed.cost_usd, replayed.latency_ms) == (
        first.response,
        first.usage,
        first.cost_usd,
        first.latency_ms,
    )


def test_recording_copies_calls_found_under_another_scope(tmp_path: Path) -> None:
    scratch = make(tmp_path, llm.LLMMode.RECORD)
    scratch.begin_run("_scratch/agent/dev", cap_usd=1.0)
    call(scratch)
    scratch.end_run()
    transport = FakeTransport()
    client = make(tmp_path, llm.LLMMode.RECORD, transport)
    client.begin_run("agent/dev", cap_usd=1.0)
    assert call(client).replayed and transport.calls == []
    client.end_run()
    (tmp_path / "cassettes/_scratch/agent/dev.jsonl").unlink()  # scratch recordings are not committed
    assert call(make(tmp_path, llm.LLMMode.REPLAY)).replayed


def test_replay_miss_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(llm.CassetteMiss, match="LLM_MODE=record"):
        call(make(tmp_path, llm.LLMMode.REPLAY))


def test_cassette_key_depends_only_on_the_request_content() -> None:
    reordered = {"messages": REQUEST["messages"], "max_tokens": 64, "model": HAIKU}
    assert request_key(reordered) == request_key(REQUEST)
    assert request_key({**REQUEST, "max_tokens": 65}) != request_key(REQUEST)


def test_cassette_holds_no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ant-test-" + "x" * 40
    monkeypatch.setenv(llm.API_KEY_VAR, secret)
    client = make(tmp_path, llm.LLMMode.RECORD)
    client.begin_run("agent/dev", cap_usd=1.0)
    call(client)
    client.end_run()
    text = (tmp_path / "cassettes/agent/dev.jsonl").read_text()
    assert secret not in text
    assert "x-api-key" not in text.lower() and "authorization" not in text.lower()
    assert set(json.loads(text.splitlines()[0])) == {
        "key",
        "label",
        "step",
        "model",
        "response",
        "usage",
        "cost_usd",
        "latency_ms",
    }


def test_every_real_call_is_written_to_the_ledger(tmp_path: Path) -> None:
    client = make(tmp_path, llm.LLMMode.LIVE)
    client.begin_run("agent/dev", cap_usd=1.0)
    c1 = call(client)
    c2 = call(client)  # live mode never reads cassettes
    ledger = json.loads((tmp_path / "spend.json").read_text())
    assert ledger["total"]["calls"] == 2
    assert ledger["total"]["cost_usd"] == pytest.approx(c1.cost_usd + c2.cost_usd)
    assert ledger["by_scope"]["agent/dev"][HAIKU]["input_tokens"] == 2000
    assert not (tmp_path / "cassettes").exists()


def test_phase_cap_stops_before_the_call(tmp_path: Path) -> None:
    transport = FakeTransport(usage={"input_tokens": 0, "output_tokens": 0})
    client = make(tmp_path, llm.LLMMode.RECORD, transport)
    assert client.ledger is not None
    client.ledger.data.cap_usd = max_cost_usd(HAIKU, llm.estimate_prompt_tokens(REQUEST), 64) / 2
    with pytest.raises(llm.BudgetExceeded) as e:
        call(client)
    assert e.value.scope == "phase" and transport.calls == []


def test_run_cap_counts_what_was_spent_in_this_run(tmp_path: Path) -> None:
    transport = FakeTransport(usage={"input_tokens": 80_000, "output_tokens": 0})  # 0.008 USD per call
    client = make(tmp_path, llm.LLMMode.LIVE, transport)
    client.begin_run("agent/dev", cap_usd=0.008)
    call(client)
    with pytest.raises(llm.BudgetExceeded) as e:
        call(client)
    assert e.value.scope == "run" and len(transport.calls) == 1


def test_case_cap_applies_in_replay_too(tmp_path: Path) -> None:
    recorder = make(tmp_path, llm.LLMMode.RECORD)
    recorder.begin_run("agent/dev", cap_usd=1.0)
    call(recorder)
    recorder.end_run()
    tiny = max_cost_usd(HAIKU, llm.estimate_prompt_tokens(REQUEST), 64) / 2
    with pytest.raises(llm.BudgetExceeded) as e:
        call(make(tmp_path, llm.LLMMode.REPLAY), cap=tiny)
    assert e.value.scope == "case"


def test_record_and_live_need_the_project_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(llm.API_KEY_VAR, raising=False)
    with pytest.raises(llm.ConfigError, match=llm.API_KEY_VAR):
        llm.AnthropicTransport()


def test_sdk_env_overrides_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(llm.API_KEY_VAR, "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Debug: 1")
    with pytest.raises(llm.ConfigError, match="ANTHROPIC_CUSTOM_HEADERS"):
        llm.AnthropicTransport()


def test_transport_ignores_the_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(llm.API_KEY_VAR, "sk-ant-project")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-other")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.delenv(llm.BASE_URL_VAR, raising=False)
    sdk = llm.AnthropicTransport()._client
    assert sdk.api_key == "sk-ant-project"
    assert str(sdk.base_url).rstrip("/") == llm.DEFAULT_BASE_URL
    monkeypatch.setenv(llm.BASE_URL_VAR, "https://proxy.example.test")
    assert str(llm.AnthropicTransport()._client.base_url).rstrip("/") == "https://proxy.example.test"


def test_mode_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "bogus")
    with pytest.raises(llm.ConfigError, match="LLM_MODE"):
        llm.LLMClient.from_env()
    monkeypatch.delenv("LLM_MODE")
    assert llm.LLMClient.from_env().mode is llm.LLMMode.REPLAY
