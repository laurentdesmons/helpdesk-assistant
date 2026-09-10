"""The local-vs-deployed seam.

The LangGraph orchestrator (Phase 4) never imports an agent directly. It asks
``build_invoker(settings, mode)`` for an :class:`AgentInvoker` and calls
``invoke_classifier`` / ``invoke_resolver`` on it. The graph topology is identical
in every mode; only the invoker changes:

- ``local``  — classifier & resolver run in-process as MAF ``Agent`` objects.
- ``remote`` — raw HTTPS to the deployed Foundry agents' Responses endpoints,
  authenticated with a ``DefaultAzureCredential`` bearer token. (Phase 4 may swap
  in ``langchain-azure-ai``'s agent node for W3C trace-context propagation.)
- ``fake``   — deterministic keyword stubs for hermetic tests.

Both agents are wired for all three modes. ``billing`` requests short-circuit to a
``policy_escalation`` ``ResolverOutput`` in every invoker, before any model or
search call (README §4: "No resolution attempt").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from opentelemetry.propagate import inject

from helpdesk.config import AgentMode, Settings
from helpdesk.contracts import (
    Category,
    Citation,
    Classification,
    ClassifierInput,
    EscalationReason,
    ResolverInput,
    ResolverOutput,
)

if TYPE_CHECKING:
    from agent_framework import Agent

    from helpdesk.search.client import KnowledgeBaseSearch

logger = logging.getLogger("helpdesk.gateway")


def _resolver_escalation(req: ResolverInput, reason: EscalationReason) -> ResolverOutput:
    return ResolverOutput(request_id=req.request_id, status="escalated", escalation_reason=reason)


class AgentInvoker(Protocol):
    async def invoke_classifier(self, req: ClassifierInput) -> Classification: ...

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput: ...

    async def aclose(self) -> None:
        """Release any async resources (the local resolver's KB clients). No-op for
        the remote and fake invokers."""
        ...


def build_invoker(settings: Settings, mode: AgentMode | None = None) -> AgentInvoker:
    mode = mode or settings.agent_mode
    if mode == "local":
        return LocalInvoker(settings)
    if mode == "fake":
        return FakeInvoker(settings)
    if mode == "remote":
        return RemoteInvoker(settings)
    raise ValueError(f"unknown agent mode: {mode!r}")


class CompositeInvoker:
    """One invoker per agent, each at its own effective mode.

    The orchestrator graph holds a single :class:`AgentInvoker`; this lets the
    classifier and resolver run at different modes in the same graph run —
    ``HELPDESK_CLASSIFIER_MODE=remote`` with the resolver still ``local``, say,
    which is how the graph is exercised against the deployed agents before the
    orchestrator itself is deployed.
    """

    def __init__(self, settings: Settings) -> None:
        c_mode = settings.mode_for("classifier")
        r_mode = settings.mode_for("resolver")
        self._classifier = build_invoker(settings, c_mode)
        # Reuse the one instance when both agents resolve to the same mode.
        self._resolver = self._classifier if r_mode == c_mode else build_invoker(settings, r_mode)

    async def invoke_classifier(self, req: ClassifierInput) -> Classification:
        return await self._classifier.invoke_classifier(req)

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput:
        return await self._resolver.invoke_resolver(req)

    async def aclose(self) -> None:
        await self._classifier.aclose()
        if self._resolver is not self._classifier:
            await self._resolver.aclose()


def build_graph_invoker(settings: Settings, mode: AgentMode | None = None) -> AgentInvoker:
    """The invoker the orchestrator graph runs on.

    ``mode`` forces both agents to one mode (the ``scripts/run_local_graph.py
    --mode`` path); omitted, each agent follows its own
    ``HELPDESK_{CLASSIFIER,RESOLVER}_MODE`` override.
    """
    if mode is not None:
        return build_invoker(settings, mode)
    return CompositeInvoker(settings)


class LocalInvoker:
    """Runs the agents in this process. Builders are imported lazily so importing
    this module doesn't pull in ``agent-framework`` for callers that only need
    ``FakeInvoker`` (e.g. ``tests/``)."""

    def __init__(self, settings: Settings) -> None:
        from helpdesk.agents.classifier.agent import build_classifier_agent

        self._settings = settings
        self._classifier = build_classifier_agent(settings)
        # Built lazily on the first non-billing resolve so classifier-only local
        # use doesn't need HELPDESK_SEARCH_ENDPOINT.
        self._resolver: Agent | None = None
        self._kb: KnowledgeBaseSearch | None = None

    async def invoke_classifier(self, req: ClassifierInput) -> Classification:
        from helpdesk.agents.classifier.agent import classify

        return await classify(self._classifier, req)

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput:
        from helpdesk.agents.resolver.agent import build_resolver_agent, resolve
        from helpdesk.search.client import build_kb_search

        if req.category == Category.billing:
            return _resolver_escalation(req, "policy_escalation")
        if self._resolver is None:
            self._kb = build_kb_search(self._settings)
            self._resolver = build_resolver_agent(self._settings, kb=self._kb)
        return await resolve(self._resolver, req)

    async def aclose(self) -> None:
        if self._kb is not None:
            await self._kb.aclose()
            self._kb = None


# Entra ID scope for invoking a deployed Foundry *agent* endpoint (the in-process
# builders use FoundryChatClient, which manages its own project-endpoint scope).
_AGENT_SCOPE = "https://ai.azure.com/.default"


def _extract_responses_text(body: dict[str, object]) -> str | None:
    """Pull the assistant text out of an OpenAI Responses payload.

    Tolerates both the convenience ``output_text`` field and the nested
    ``output[].content[].text`` walk, since the hosting layer is still prerelease.
    """
    top = body.get("output_text")
    if isinstance(top, str) and top.strip():
        return top
    parts: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)
    return "".join(parts) or None


class RemoteInvoker:
    """Calls the deployed Foundry agents over their Responses endpoints."""

    def __init__(self, settings: Settings) -> None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        self._settings = settings
        self._classifier_url = settings.classifier_responses_url()
        # Resolved lazily so a classifier-only remote caller needn't configure it.
        self._resolver_url: str | None = None
        self._token = get_bearer_token_provider(DefaultAzureCredential(), _AGENT_SCOPE)

    # Attempts share one budget: transient server errors (model overload/throttle),
    # httpx transport errors (cross-region read timeouts), and unparseable
    # responses all consume a try, with a short backoff between.
    _MAX_ATTEMPTS = 4
    _BACKOFF_SECONDS = 2.0

    async def invoke_classifier(self, req: ClassifierInput) -> Classification:
        import asyncio

        import httpx

        from helpdesk.agents.classifier.agent import JSON_ONLY_SUFFIX, _parse_text, build_prompt

        prompt = build_prompt(req)
        # W3C trace context: chain the deployed classifier's spans under the
        # orchestrator node span (README §2 — "not assumed"). No-op when no span
        # is active (local/fake, or tracing disabled).
        headers = {"Authorization": f"Bearer {self._token()}"}
        inject(headers)
        last_error = "no attempts made"

        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(1, self._MAX_ATTEMPTS + 1):
                if attempt > 1:
                    await asyncio.sleep(self._BACKOFF_SECONDS * (attempt - 1))

                try:
                    resp = await client.post(
                        self._classifier_url,
                        json={"input": prompt, "stream": False},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                except httpx.HTTPError as exc:  # timeout / connect / 5xx — transient
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("remote classifier attempt %d: %s", attempt, last_error)
                    continue
                logger.debug("remote classifier raw response (attempt %d): %s", attempt, body)

                if body.get("status") == "failed":
                    err = body.get("error") or {}
                    last_error = (
                        f"run failed [{err.get('code')}]: {err.get('message')} "
                        f"(response id {body.get('id')}, session {body.get('agent_session_id')})"
                    )
                    logger.warning("remote classifier attempt %d: %s", attempt, last_error)
                    continue  # transient — retry with backoff

                text = _extract_responses_text(body)
                logger.debug("remote classifier extracted text: %r", text)
                parsed = _parse_text(text)
                if parsed is not None:
                    return parsed

                last_error = f"unparseable response text: {text!r}"
                prompt = build_prompt(req) + JSON_ONLY_SUFFIX

        raise RuntimeError(
            f"remote classifier failed for {req.request_id!r} after {self._MAX_ATTEMPTS} attempts; "
            f"last: {last_error}"
        )

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput:
        import asyncio

        import httpx

        from helpdesk.agents.resolver.agent import (
            JSON_ONLY_SUFFIX,
            _guard,
            _parse_text,
            build_prompt,
        )

        if req.category == Category.billing:
            return _resolver_escalation(req, "policy_escalation")

        if self._resolver_url is None:
            self._resolver_url = self._settings.resolver_responses_url()
        prompt = build_prompt(req)
        # W3C trace context — see invoke_classifier. No-op without an active span.
        headers = {"Authorization": f"Bearer {self._token()}"}
        inject(headers)
        last_error = "no attempts made"

        # Tool call + query embedding + Search + a second model turn — much slower
        # than the single-shot classifier.
        async with httpx.AsyncClient(timeout=120) as client:
            for attempt in range(1, self._MAX_ATTEMPTS + 1):
                if attempt > 1:
                    await asyncio.sleep(self._BACKOFF_SECONDS * (attempt - 1))

                try:
                    resp = await client.post(
                        self._resolver_url,
                        json={"input": prompt, "stream": False},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                except httpx.HTTPError as exc:  # timeout / connect / 5xx — transient
                    last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("remote resolver attempt %d: %s", attempt, last_error)
                    continue
                logger.debug("remote resolver raw response (attempt %d): %s", attempt, body)

                if body.get("status") == "failed":
                    err = body.get("error") or {}
                    last_error = (
                        f"run failed [{err.get('code')}]: {err.get('message')} "
                        f"(response id {body.get('id')}, session {body.get('agent_session_id')})"
                    )
                    logger.warning("remote resolver attempt %d: %s", attempt, last_error)
                    continue  # transient — retry with backoff

                text = _extract_responses_text(body)
                logger.debug("remote resolver extracted text: %r", text)
                parsed = _parse_text(text, req)
                if parsed is not None:
                    return _guard(parsed, req)

                last_error = f"unparseable response text: {text!r}"
                prompt = build_prompt(req) + JSON_ONLY_SUFFIX

        raise RuntimeError(
            f"remote resolver failed for {req.request_id!r} after {self._MAX_ATTEMPTS} attempts; "
            f"last: {last_error}"
        )

    async def aclose(self) -> None:
        return None


_KEYWORDS: dict[Category, tuple[str, ...]] = {
    Category.billing: ("charge", "invoice", "refund", "subscription", "payment", "license"),
    Category.hr: ("leave", "pto", "vacation", "benefit", "payroll", "paycheck", "parental"),
    Category.support: ("vpn", "password", "login", "laptop", "install", "email", "network"),
}

# Per-KB keyword -> real ``docs/`` doc_id (stem) for the fake resolver. A hit means
# "the KB can answer this"; the doc_id is a genuine chunk id so citation-validity
# checks pass in fake mode too.
_KB_DOC_FOR: dict[str, dict[str, str]] = {
    "support": {
        "vpn": "vpn-troubleshooting",
        "password": "password-reset-policy",
        "login": "password-reset-policy",
        "mfa": "password-reset-policy",
        "install": "software-install-requests",
        "software": "software-install-requests",
        "laptop": "software-install-requests",
    },
    "hr": {
        "pto": "pto-policy",
        "vacation": "pto-policy",
        "carryover": "pto-policy",
        "leave": "parental-leave-policy",
        "parental": "parental-leave-policy",
        "benefit": "benefits-enrollment-guide",
        "enroll": "benefits-enrollment-guide",
    },
}


class FakeInvoker:
    """Deterministic keyword classifier for hermetic tests — never touches a model."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def invoke_classifier(self, req: ClassifierInput) -> Classification:
        text = req.message_text.lower()
        for category, words in _KEYWORDS.items():
            hits = [w for w in words if w in text]
            if hits:
                return Classification(
                    category=category,
                    # >= 0.85 on a single keyword hit — a clear match is not
                    # low-confidence, so the orchestrator routes it to resolve
                    # rather than escalating it.
                    confidence=min(0.95, 0.75 + 0.1 * len(hits)),
                    rationale=f"matched keywords: {', '.join(hits)}",
                )
        return Classification(
            category=Category.support,
            confidence=0.3,
            rationale="no keyword match; defaulting to support with low confidence",
        )

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput:
        if req.category == Category.billing:
            return _resolver_escalation(req, "policy_escalation")
        text = req.message_text.lower()
        cat = str(req.category)
        hits = [(w, doc) for w, doc in _KB_DOC_FOR.get(cat, {}).items() if w in text]
        if hits:
            keyword, doc_id = hits[0]
            return ResolverOutput(
                request_id=req.request_id,
                status="answered",
                answer=f"[fake] See the {cat} knowledge base regarding {keyword}.",
                citations=[
                    Citation(
                        doc_id=doc_id,
                        title=f"{cat} policy > {keyword}",
                        snippet=f"fake snippet about {keyword}",
                        score=0.9,
                    )
                ],
            )
        return _resolver_escalation(req, "not_grounded")

    async def aclose(self) -> None:
        return None
