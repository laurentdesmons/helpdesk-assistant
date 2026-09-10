"""Agent-gateway seam: FakeInvoker, build_invoker dispatch, RemoteInvoker wire
envelope, and the config alias bridge. Hermetic — no network, no real credential."""

from __future__ import annotations

import json
from typing import Any

import pytest

from helpdesk.agent_gateway import (
    FakeInvoker,
    LocalInvoker,
    RemoteInvoker,
    _extract_responses_text,
    build_invoker,
)
from helpdesk.config import Settings, get_settings
from helpdesk.contracts import Category, Classification, ClassifierInput


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
