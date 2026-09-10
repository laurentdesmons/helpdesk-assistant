"""Resolver wiring in the agent gateway: FakeInvoker behaviour, RemoteInvoker wire
envelope + billing short-circuit, and the resolver URL / alias plumbing. Hermetic."""

from __future__ import annotations

import json
from typing import Any

import pytest

from helpdesk.agent_gateway import FakeInvoker, RemoteInvoker
from helpdesk.config import Settings
from helpdesk.contracts import ResolverInput, ResolverOutput


def _req(text: str, category: str = "support", rid: str = "t1") -> ResolverInput:
    return ResolverInput(request_id=rid, category=category, message_text=text)


# --------------------------------------------------------------------------- #
# FakeInvoker
# --------------------------------------------------------------------------- #
async def test_fake_resolver_billing_escalates() -> None:
    out = await FakeInvoker(Settings(_env_file=None)).invoke_resolver(  # type: ignore[call-arg]
        _req("refund the duplicate charge", category="billing")
    )
    assert out.status == "escalated"
    assert out.escalation_reason == "policy_escalation"


async def test_fake_resolver_keyword_hit_answers_with_citation() -> None:
    out = await FakeInvoker(Settings(_env_file=None)).invoke_resolver(  # type: ignore[call-arg]
        _req("my vpn keeps dropping")
    )
    assert out.status == "answered"
    assert out.answer
    assert len(out.citations) == 1


async def test_fake_resolver_no_keyword_not_grounded() -> None:
    out = await FakeInvoker(Settings(_env_file=None)).invoke_resolver(  # type: ignore[call-arg]
        _req("please recommend a lunch spot near the office")
    )
    assert out.status == "escalated"
    assert out.escalation_reason == "not_grounded"


# --------------------------------------------------------------------------- #
# RemoteInvoker
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def _remote(monkeypatch: pytest.MonkeyPatch) -> Any:
    import azure.identity

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(
        azure.identity, "get_bearer_token_provider", lambda *a, **k: (lambda: "fake-token")
    )

    calls: list[dict[str, Any]] = []
    answered = {
        "request_id": "t1",
        "status": "answered",
        "answer": "Restart the VPN client and retry.",
        "citations": [{"doc_id": "vpn-troubleshooting", "title": "VPN > Steps"}],
    }
    replies: list[dict[str, Any]] = [{"output_text": json.dumps(answered)}]

    async def fake_post(self: Any, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        return _FakeResponse(replies[min(len(calls) - 1, len(replies) - 1)])

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    monkeypatch.setattr(RemoteInvoker, "_BACKOFF_SECONDS", 0.0)

    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        classifier_agent_endpoint="https://acct.services.ai.azure.com/agents/helpdesk-classifier/endpoint/protocols/openai/responses",
        resolver_agent_endpoint="https://acct.services.ai.azure.com/agents/helpdesk-resolver/endpoint/protocols/openai/responses",
    )
    return RemoteInvoker(s), calls, replies


async def test_remote_resolver_request_and_parse(_remote: Any) -> None:
    from helpdesk.agents.resolver.agent import build_prompt

    invoker, calls, _replies = _remote
    req = _req("my vpn keeps dropping every few minutes")
    res = await invoker.invoke_resolver(req)

    assert isinstance(res, ResolverOutput)
    assert res.status == "answered"
    assert res.citations[0].doc_id == "vpn-troubleshooting"

    assert len(calls) == 1
    assert calls[0]["url"].endswith("/helpdesk-resolver/endpoint/protocols/openai/responses")
    assert calls[0]["headers"]["Authorization"] == "Bearer fake-token"
    assert calls[0]["json"] == {"input": build_prompt(req), "stream": False}


async def test_remote_resolver_billing_makes_no_http_call(_remote: Any) -> None:
    invoker, calls, _replies = _remote
    res = await invoker.invoke_resolver(_req("refund my double charge", category="billing"))
    assert res.status == "escalated"
    assert res.escalation_reason == "policy_escalation"
    assert calls == []


async def test_remote_resolver_retries_on_unparseable(_remote: Any) -> None:
    invoker, calls, replies = _remote
    replies.clear()
    escalated = {"request_id": "t1", "status": "escalated", "escalation_reason": "not_grounded"}
    replies.append({"output_text": "I cannot help with that"})
    replies.append({"output_text": json.dumps(escalated)})
    res = await invoker.invoke_resolver(_req("how do I 3d-print a bracket"))
    assert res.status == "escalated"
    assert len(calls) == 2
    assert "Return ONLY the JSON object" in calls[1]["json"]["input"]


async def test_remote_resolver_downgrades_answered_without_citations(_remote: Any) -> None:
    invoker, _calls, replies = _remote
    replies.clear()
    answered = {"request_id": "t1", "status": "answered", "answer": "just do it", "citations": []}
    replies.append({"output_text": json.dumps(answered)})
    res = await invoker.invoke_resolver(_req("vpn down"))
    assert res.status == "escalated"
    assert res.escalation_reason == "not_grounded"


# --------------------------------------------------------------------------- #
# resolver_responses_url — same idempotency contract as the classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (
            "https://a.services.ai.azure.com/api/projects/p/agents/helpdesk-resolver/endpoint/protocols/openai/responses?api-version=v1",
            "https://a.services.ai.azure.com/api/projects/p/agents/helpdesk-resolver/endpoint/protocols/openai/responses?api-version=v1",
        ),
        (
            "https://a.services.ai.azure.com/api/projects/p/agents/r/endpoint/protocols/openai",
            "https://a.services.ai.azure.com/api/projects/p/agents/r/endpoint/protocols/openai/responses",
        ),
    ],
)
def test_resolver_responses_url_is_idempotent(endpoint: str, expected: str) -> None:
    assert (
        Settings(_env_file=None, resolver_agent_endpoint=endpoint).resolver_responses_url()  # type: ignore[call-arg]
        == expected
    )


def test_resolver_agent_endpoint_alias_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELPDESK_RESOLVER_AGENT_ENDPOINT", raising=False)
    monkeypatch.setenv(
        "AGENT_RESOLVER_RESPONSES_ENDPOINT",
        "https://acct.services.ai.azure.com/agents/helpdesk-resolver/endpoint/protocols/openai/responses?api-version=v1",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.resolver_agent_endpoint is not None
    assert s.resolver_responses_url().endswith("responses?api-version=v1")
