"""Resolver agent run-helper + search tool. Hermetic — a scripted fake agent and a
stub KB, no network, no real credential."""

from __future__ import annotations

import json
from typing import Any

from helpdesk.agents.resolver import agent as ra
from helpdesk.contracts import ResolverInput, ResolverOutput
from helpdesk.search.client import SearchResult


def _req(text: str = "my vpn keeps dropping", category: str = "support", rid: str = "t1") -> ResolverInput:
    return ResolverInput(request_id=rid, category=category, message_text=text)


class _Response:
    def __init__(self, *, value: Any = None, text: str | None = None) -> None:
        self.value = value
        self.text = text


class _FakeAgent:
    """Returns scripted responses; records prompts; can raise."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def run(self, prompt: str, *, options: Any = None) -> Any:
        self.prompts.append(prompt)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# --------------------------------------------------------------------------- #
# resolve() — routing
# --------------------------------------------------------------------------- #
async def test_billing_short_circuits_without_calling_the_model() -> None:
    agent = _FakeAgent()  # no scripted responses — .run must never be called
    out = await ra.resolve(agent, _req("refund my double charge", category="billing"))
    assert out.status == "escalated"
    assert out.escalation_reason == "policy_escalation"
    assert agent.prompts == []


async def test_native_structured_value_passes_through() -> None:
    draft = ra._ResolverDraft(
        request_id="t1",
        status="answered",
        answer="Restart the VPN client and retry.",
        citations=[{"doc_id": "vpn-troubleshooting", "title": "VPN > Steps"}],
    )
    agent = _FakeAgent(_Response(value=draft))
    out = await ra.resolve(agent, _req())
    assert isinstance(out, ResolverOutput)
    assert out.status == "answered"
    assert out.citations[0].doc_id == "vpn-troubleshooting"


async def test_text_json_path_defaults_request_id() -> None:
    body = json.dumps(
        {"status": "answered", "answer": "Reset via the portal.", "citations": [{"doc_id": "d"}]}
    )
    agent = _FakeAgent(_Response(text=body))
    out = await ra.resolve(agent, _req(rid="req-99"))
    assert out.request_id == "req-99"
    assert out.status == "answered"


async def test_answered_without_citations_is_downgraded() -> None:
    draft = ra._ResolverDraft(request_id="t1", status="answered", answer="do X", citations=[])
    agent = _FakeAgent(_Response(value=draft))
    out = await ra.resolve(agent, _req())
    assert out.status == "escalated"
    assert out.escalation_reason == "not_grounded"


async def test_unparseable_then_retry_succeeds() -> None:
    good = json.dumps(
        {"request_id": "t1", "status": "escalated", "escalation_reason": "not_grounded"}
    )
    agent = _FakeAgent(_Response(text="sorry, I can't"), _Response(text=good))
    out = await ra.resolve(agent, _req())
    assert out.status == "escalated"
    assert len(agent.prompts) == 2
    assert "Return ONLY the JSON object" in agent.prompts[1]


async def test_both_attempts_fail_falls_back_to_not_grounded() -> None:
    agent = _FakeAgent(_Response(text="nope"), RuntimeError("model exploded"))
    out = await ra.resolve(agent, _req())
    assert out.status == "escalated"
    assert out.escalation_reason == "not_grounded"


def test_build_prompt_format() -> None:
    assert ra.build_prompt(_req("vpn down", rid="r5")) == (
        "request_id: r5\nCategory: support\nUser request:\nvpn down"
    )


# --------------------------------------------------------------------------- #
# make_search_tool()
# --------------------------------------------------------------------------- #
class _StubKB:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises

    async def search(self, category: str, query: str, top_k: int | None = None) -> list[SearchResult]:
        if self._raises:
            raise ValueError("no knowledge-base index for category 'billing'")
        return [
            SearchResult(
                chunk_id="c1",
                doc_id="vpn-troubleshooting",
                title="VPN Connection Troubleshooting",
                section="Steps",
                content="Restart the VPN client and reattempt. " * 20,
                source_path="docs/vpn-troubleshooting.md",
                score=1.1,
                reranker_score=2.8,
            )
        ]


async def test_search_tool_returns_json_results() -> None:
    tool = ra.make_search_tool(_StubKB())
    assert tool.name == "search_knowledge_base"

    out = await tool.invoke(arguments={"query": "vpn drops", "category": "support"})
    payload = json.loads(out[0].text)
    assert payload["count"] == 1
    assert payload["results"][0]["doc_id"] == "vpn-troubleshooting"
    assert payload["results"][0]["title"] == "VPN Connection Troubleshooting > Steps"
    assert len(payload["results"][0]["snippet"]) <= ra._MAX_SNIPPET


async def test_search_tool_reports_bad_category() -> None:
    tool = ra.make_search_tool(_StubKB(raises=True))
    out = await tool.invoke(arguments={"query": "x", "category": "billing"})
    payload = json.loads(out[0].text)
    assert payload["count"] == 0
    assert "error" in payload
