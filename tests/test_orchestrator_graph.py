"""Orchestrator graph: routing matrix, HelpdeskResult shape, escalation-record
writes, and the host shim. Hermetic — FakeInvoker, tmp_path escalation store."""

from __future__ import annotations

from pathlib import Path

import pytest

from helpdesk.agent_gateway import FakeInvoker
from helpdesk.agents.orchestrator.graph import build_graph, run_graph
from helpdesk.agents.orchestrator.host import _GraphAgent, _to_request
from helpdesk.config import Settings
from helpdesk.contracts import ClassifierInput, HelpdeskResult
from helpdesk.escalation import JSONFileEscalationStore


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        escalation_store_path=str(tmp_path / "escalations.json"),
        tracing_enabled=False,
    )


def _req(text: str, rid: str = "t1") -> ClassifierInput:
    return ClassifierInput(request_id=rid, user_id="u", message_text=text, channel="portal")


class _SpyInvoker(FakeInvoker):
    """FakeInvoker that counts resolver calls."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.resolver_calls = 0

    async def invoke_resolver(self, req):  # type: ignore[no-untyped-def]
        self.resolver_calls += 1
        return await super().invoke_resolver(req)


async def _run(settings: Settings, text: str, *, invoker: FakeInvoker | None = None) -> HelpdeskResult:
    invoker = invoker or FakeInvoker(settings)
    graph = build_graph(settings, invoker=invoker)
    return await run_graph(graph, _req(text))


# --------------------------------------------------------------------------- #
# routing matrix
# --------------------------------------------------------------------------- #
async def test_support_answered(settings: Settings) -> None:
    res = await _run(settings, "my vpn keeps dropping every few minutes")
    assert res.outcome == "answered"
    assert res.category == "support"
    assert res.answer and res.citations
    assert res.classification is not None
    # round-trips through JSON like every other contract
    assert HelpdeskResult.model_validate_json(res.model_dump_json()) == res


async def test_billing_escalates_policy(settings: Settings) -> None:
    invoker = _SpyInvoker(settings)
    res = await _run(settings, "I was charged twice, please refund one", invoker=invoker)
    assert res.outcome == "escalated"
    assert res.escalation_reason == "policy_escalation"
    assert res.category == "billing"
    # billing still runs through the resolve node (the seam short-circuits it)
    assert invoker.resolver_calls == 1
    rec = await JSONFileEscalationStore(settings.escalation_store_path).get("t1")
    assert rec is not None and rec.escalation_reason == "policy_escalation"


async def test_low_confidence_escalates_without_resolver(settings: Settings) -> None:
    invoker = _SpyInvoker(settings)
    res = await _run(settings, "hi", invoker=invoker)
    assert res.outcome == "escalated"
    assert res.escalation_reason == "low_confidence"
    assert invoker.resolver_calls == 0  # never reached resolve
    rec = await JSONFileEscalationStore(settings.escalation_store_path).get("t1")
    assert rec is not None and rec.escalation_reason == "low_confidence"


async def test_off_kb_support_not_grounded(settings: Settings) -> None:
    res = await _run(settings, "my office network printer keeps going offline")
    assert res.outcome == "escalated"
    assert res.escalation_reason == "not_grounded"
    assert res.category == "support"


async def test_no_escalation_record_when_answered(settings: Settings) -> None:
    await _run(settings, "how much pto do I have left this year")
    assert not Path(settings.escalation_store_path).exists()


# --------------------------------------------------------------------------- #
# route edge threshold
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("confidence", "expected_outcome"),
    [(0.79, "escalated"), (0.80, "answered"), (0.95, "answered")],
)
async def test_route_edge_threshold(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, confidence: float, expected_outcome: str
) -> None:
    from helpdesk.contracts import Classification

    invoker = FakeInvoker(settings)

    async def fixed_classifier(req):  # type: ignore[no-untyped-def]
        return Classification(category="support", confidence=confidence, rationale="x")

    monkeypatch.setattr(invoker, "invoke_classifier", fixed_classifier)
    graph = build_graph(settings, invoker=invoker)
    res = await run_graph(graph, _req("my vpn keeps dropping"))
    assert res.outcome == expected_outcome


# --------------------------------------------------------------------------- #
# host shim
# --------------------------------------------------------------------------- #
def test_to_request_parses_json() -> None:
    req = _to_request('{"request_id": "r9", "user_id": "u9", "message_text": "vpn down", "channel": "teams"}')
    assert (req.request_id, req.user_id, req.message_text, req.channel) == ("r9", "u9", "vpn down", "teams")


def test_to_request_tolerates_raw_text() -> None:
    req = _to_request("my vpn is broken")
    assert req.message_text == "my vpn is broken"
    assert req.request_id  # synthesised


async def test_graph_agent_run_returns_result_json(settings: Settings) -> None:
    agent = _GraphAgent(
        settings.model_copy(update={"agent_mode": "fake"})
    )
    resp = await agent.run(
        '{"request_id": "h1", "user_id": "u", "message_text": "my vpn keeps dropping", "channel": "portal"}'
    )
    payload = HelpdeskResult.model_validate_json(resp.messages[0].text)
    assert payload.request_id == "h1"
    assert payload.outcome == "answered"
