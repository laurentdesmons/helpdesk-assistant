"""Agent-gateway seam: FakeInvoker, build_invoker dispatch, RemoteInvoker wire
envelope, and the config alias bridge. Hermetic — no network, no real credential."""

from __future__ import annotations

import json
from typing import Any

import pytest

from helpdesk.agent_gateway import (
    FakeInvoker,
    LocalInvoker,
    OrchestratorClient,
    RemoteInvoker,
    _extract_responses_text,
    _parse_helpdesk_result,
    build_invoker,
)
from helpdesk.config import Settings, get_settings
from helpdesk.contracts import Category, Classification, ClassifierInput, HelpdeskResult


def _req(text: str, rid: str = "t1") -> ClassifierInput:
    return ClassifierInput(request_id=rid, user_id="test", message_text=text, channel="portal")


# --------------------------------------------------------------------------- #
# FakeInvoker
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected", "keyword"),
    [
        ("please refund the duplicate charge on my invoice", Category.billing, "refund"),
        ("my vpn keeps dropping when I login", Category.support, "vpn"),
        ("how much pto do I have left this year", Category.hr, "pto"),
    ],
)
async def test_fake_invoker_keyword_hit(text: str, expected: Category, keyword: str) -> None:
    res = await FakeInvoker(Settings()).invoke_classifier(_req(text))
    assert res.category == expected
    assert 0.6 <= res.confidence <= 0.95
    assert keyword in res.rationale


async def test_fake_invoker_no_keyword_defaults_to_support() -> None:
    res = await FakeInvoker(Settings()).invoke_classifier(_req("hello there, a general question"))
    assert res.category == Category.support
    assert res.confidence == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# build_invoker dispatch
# --------------------------------------------------------------------------- #
@pytest.fixture
def _no_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the bits of LocalInvoker/RemoteInvoker that would touch Azure."""
    import azure.identity

    monkeypatch.setattr("helpdesk.agents.classifier.agent.build_classifier_agent", lambda s: object())
    monkeypatch.setattr(
        "helpdesk.agents.resolver.agent.build_resolver_agent", lambda *a, **k: object()
    )
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda *a, **k: (lambda: "tok"))


@pytest.mark.usefixtures("_no_azure")
def test_build_invoker_dispatch() -> None:
    s = Settings(classifier_agent_endpoint="https://x.services.ai.azure.com/agents/c/endpoint/protocols/openai/responses")
    assert isinstance(build_invoker(s, "local"), LocalInvoker)
    assert isinstance(build_invoker(s, "fake"), FakeInvoker)
    assert isinstance(build_invoker(s, "remote"), RemoteInvoker)


def test_build_invoker_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown agent mode"):
        build_invoker(Settings(), "bogus")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# RemoteInvoker wire envelope
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # noqa: D401
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def _remote(monkeypatch: pytest.MonkeyPatch) -> Any:
    import azure.identity

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda *a, **k: (lambda: "fake-token"))

    calls: list[dict[str, Any]] = []
    _billing = {"category": "billing", "confidence": 0.92, "rationale": "duplicate charge"}
    replies: list[dict[str, Any]] = [{"output_text": json.dumps(_billing)}]

    async def fake_post(self: Any, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        return _FakeResponse(replies[min(len(calls) - 1, len(replies) - 1)])

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    monkeypatch.setattr(RemoteInvoker, "_BACKOFF_SECONDS", 0.0)

    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        classifier_agent_endpoint=(
            "https://acct.services.ai.azure.com/agents/helpdesk-classifier"
            "/endpoint/protocols/openai/responses"
        ),
    )
    return RemoteInvoker(s), calls, replies


async def test_remote_invoker_request_and_parse(_remote: Any) -> None:
    invoker, calls, _replies = _remote
    from helpdesk.agents.classifier.agent import build_prompt

    req = _req("I was charged twice this month")
    res = await invoker.invoke_classifier(req)

    assert isinstance(res, Classification)
    assert res.category == Category.billing
    assert res.confidence == pytest.approx(0.92)

    assert len(calls) == 1
    assert calls[0]["url"].endswith("/responses")
    assert calls[0]["headers"]["Authorization"] == "Bearer fake-token"
    assert calls[0]["json"] == {"input": build_prompt(req), "stream": False}


async def test_remote_invoker_retries_on_unparseable(_remote: Any) -> None:
    invoker, calls, replies = _remote
    replies.clear()
    replies.append({"output_text": "sorry, I cannot help"})
    replies.append(
        {"output_text": json.dumps({"category": "support", "confidence": 0.8, "rationale": "vpn issue"})}
    )

    res = await invoker.invoke_classifier(_req("vpn down"))
    assert res.category == Category.support
    assert len(calls) == 2
    assert "Return ONLY the JSON object" in calls[1]["json"]["input"]


def test_extract_responses_text_nested_walk() -> None:
    body = {"output": [{"content": [{"type": "output_text", "text": '{"a": 1}'}]}]}
    assert _extract_responses_text(body) == '{"a": 1}'
    assert _extract_responses_text({"output": []}) is None


async def test_remote_invoker_retries_on_transport_error(
    _remote: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    invoker, calls, _replies = _remote
    real_post = httpx.AsyncClient.post
    state = {"first": True}

    async def flaky_post(self: Any, url: str, **kwargs: Any) -> Any:
        if state["first"]:
            state["first"] = False
            raise httpx.ReadTimeout("cross-region stall")
        return await real_post(self, url, **kwargs)

    monkeypatch.setattr("httpx.AsyncClient.post", flaky_post)
    res = await invoker.invoke_classifier(_req("I was charged twice"))
    assert res.category == Category.billing
    assert len(calls) == 1  # the successful retry


async def test_remote_invoker_injects_trace_context(_remote: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    invoker, _calls, _replies = _remote
    seen: list[dict[str, str]] = []
    monkeypatch.setattr("helpdesk.agent_gateway.inject", lambda carrier: seen.append(carrier))

    await invoker.invoke_classifier(_req("I was charged twice"))

    assert len(seen) == 1
    assert seen[0]["Authorization"] == "Bearer fake-token"  # inject got the real header dict


# --------------------------------------------------------------------------- #
# CompositeInvoker / build_graph_invoker
# --------------------------------------------------------------------------- #
def test_composite_invoker_reuses_one_instance_when_modes_match() -> None:
    from helpdesk.agent_gateway import CompositeInvoker

    ci = CompositeInvoker(Settings(_env_file=None, agent_mode="fake"))  # type: ignore[call-arg]
    assert ci._resolver is ci._classifier
    assert isinstance(ci._classifier, FakeInvoker)


@pytest.mark.usefixtures("_no_azure")
def test_composite_invoker_splits_on_per_agent_mode() -> None:
    from helpdesk.agent_gateway import CompositeInvoker

    ci = CompositeInvoker(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            classifier_mode="fake",
            resolver_mode="local",
            foundry_project_endpoint="https://x.services.ai.azure.com/api/projects/p",
        )
    )
    assert isinstance(ci._classifier, FakeInvoker)
    assert isinstance(ci._resolver, LocalInvoker)


def test_build_graph_invoker_dispatch() -> None:
    from helpdesk.agent_gateway import CompositeInvoker, build_graph_invoker

    s = Settings(_env_file=None, agent_mode="fake")  # type: ignore[call-arg]
    assert isinstance(build_graph_invoker(s, "fake"), FakeInvoker)
    assert isinstance(build_graph_invoker(s), CompositeInvoker)


async def test_composite_invoker_aclose_closes_both() -> None:
    from helpdesk.agent_gateway import CompositeInvoker

    closed: list[str] = []

    class _Closer:
        def __init__(self, tag: str) -> None:
            self._tag = tag

        async def aclose(self) -> None:
            closed.append(self._tag)

    ci = CompositeInvoker(Settings(_env_file=None, agent_mode="fake"))  # type: ignore[call-arg]
    ci._classifier = _Closer("c")  # type: ignore[assignment]
    ci._resolver = _Closer("r")  # type: ignore[assignment]
    await ci.aclose()
    assert closed == ["c", "r"]


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        # azd writes AGENT_CLASSIFIER_RESPONSES_ENDPOINT fully-formed, with a query string
        (
            "https://a.services.ai.azure.com/api/projects/p/agents/helpdesk-classifier"
            "/endpoint/protocols/openai/responses?api-version=v1",
            "https://a.services.ai.azure.com/api/projects/p/agents/helpdesk-classifier"
            "/endpoint/protocols/openai/responses?api-version=v1",
        ),
        # bare base URL gets /responses appended
        (
            "https://a.services.ai.azure.com/api/projects/p/agents/c/endpoint/protocols/openai",
            "https://a.services.ai.azure.com/api/projects/p/agents/c/endpoint/protocols/openai/responses",
        ),
    ],
)
def test_classifier_responses_url_is_idempotent(endpoint: str, expected: str) -> None:
    assert Settings(_env_file=None, classifier_agent_endpoint=endpoint).classifier_responses_url() == expected  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# OrchestratorClient (deployed-orchestrator client, not an AgentInvoker)
# --------------------------------------------------------------------------- #
def test_parse_helpdesk_result_fills_request_id() -> None:
    req = ClassifierInput(request_id="o1", user_id="u", message_text="x", channel="portal")
    out = _parse_helpdesk_result('{"outcome": "escalated", "escalation_reason": "low_confidence"}', req)
    assert isinstance(out, HelpdeskResult)
    assert out.request_id == "o1" and out.outcome == "escalated"
    assert _parse_helpdesk_result("not json", req) is None


@pytest.fixture
def _orch(monkeypatch: pytest.MonkeyPatch) -> Any:
    import azure.identity

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(
        azure.identity, "get_bearer_token_provider", lambda *a, **k: (lambda: "fake-token")
    )
    calls: list[dict[str, Any]] = []
    answered = {
        "request_id": "o1",
        "outcome": "answered",
        "category": "support",
        "answer": "restart the client",
        "citations": [{"doc_id": "vpn-troubleshooting"}],
    }
    replies: list[dict[str, Any]] = [{"output_text": json.dumps(answered)}]

    async def fake_post(self: Any, url: str, **kwargs: Any) -> _FakeResponse:
        calls.append({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        return _FakeResponse(replies[min(len(calls) - 1, len(replies) - 1)])

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    monkeypatch.setattr(OrchestratorClient, "_BACKOFF_SECONDS", 0.0)
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        orchestrator_agent_endpoint="https://a.services.ai.azure.com/agents/helpdesk-orchestrator/endpoint/protocols/openai/responses",
    )
    return OrchestratorClient(s), calls


async def test_orchestrator_client_request_and_parse(_orch: Any) -> None:
    client, calls = _orch
    req = ClassifierInput(request_id="o1", user_id="u", message_text="vpn down", channel="portal")
    res = await client.run(req)

    assert isinstance(res, HelpdeskResult)
    assert res.outcome == "answered" and res.citations[0].doc_id == "vpn-troubleshooting"
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/helpdesk-orchestrator/endpoint/protocols/openai/responses")
    assert calls[0]["headers"]["Authorization"] == "Bearer fake-token"
    assert calls[0]["json"] == {"input": req.model_dump_json(), "stream": False}


# --------------------------------------------------------------------------- #
# config alias bridge — platform injects un-prefixed names
# --------------------------------------------------------------------------- #
def test_settings_reads_unprefixed_platform_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELPDESK_FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://acct.services.ai.azure.com/api/projects/p")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc")
    get_settings.cache_clear()

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.foundry_project_endpoint == "https://acct.services.ai.azure.com/api/projects/p"
    assert s.applicationinsights_connection_string == "InstrumentationKey=abc"
    get_settings.cache_clear()
