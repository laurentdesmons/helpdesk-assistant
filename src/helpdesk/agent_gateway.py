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

Phase 1 (+ its deploy step) wires the classifier for ``local``, ``remote`` and
``fake``. The resolver lands in Phase 3.
"""

from __future__ import annotations

import logging
from typing import Protocol

from helpdesk.config import AgentMode, Settings
from helpdesk.contracts import (
    Category,
    Classification,
    ClassifierInput,
    ResolverInput,
    ResolverOutput,
)

logger = logging.getLogger("helpdesk.gateway")


class AgentInvoker(Protocol):
    async def invoke_classifier(self, req: ClassifierInput) -> Classification: ...

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput: ...


def build_invoker(settings: Settings, mode: AgentMode | None = None) -> AgentInvoker:
    mode = mode or settings.agent_mode
    if mode == "local":
        return LocalInvoker(settings)
    if mode == "fake":
        return FakeInvoker(settings)
    if mode == "remote":
        return RemoteInvoker(settings)
    raise ValueError(f"unknown agent mode: {mode!r}")


class LocalInvoker:
    """Runs the agents in this process. Builders are imported lazily so importing
    this module doesn't pull in ``agent-framework`` for callers that only need
    ``FakeInvoker`` (e.g. ``tests/``)."""

    def __init__(self, settings: Settings) -> None:
        from helpdesk.agents.classifier.agent import build_classifier_agent

        self._settings = settings
        self._classifier = build_classifier_agent(settings)
        self._resolver = None  # Phase 3

    async def invoke_classifier(self, req: ClassifierInput) -> Classification:
        from helpdesk.agents.classifier.agent import classify

        return await classify(self._classifier, req)

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput:
        raise NotImplementedError("resolver lands in Phase 3")


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
        self._url = settings.classifier_responses_url()
        self._token = get_bearer_token_provider(DefaultAzureCredential(), _AGENT_SCOPE)

    # Attempts share one budget: transient server errors (model overload/throttle)
    # and unparseable responses both consume a try, with a short backoff between.
    _MAX_ATTEMPTS = 4
    _BACKOFF_SECONDS = 2.0

    async def invoke_classifier(self, req: ClassifierInput) -> Classification:
        import asyncio

        import httpx

        from helpdesk.agents.classifier.agent import JSON_ONLY_SUFFIX, _parse_text, build_prompt

        prompt = build_prompt(req)
        # TODO(phase4): inject W3C trace context (traceparent/tracestate) here.
        headers = {"Authorization": f"Bearer {self._token()}"}
        last_error = "no attempts made"

        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(1, self._MAX_ATTEMPTS + 1):
                if attempt > 1:
                    await asyncio.sleep(self._BACKOFF_SECONDS * (attempt - 1))

                resp = await client.post(
                    self._url, json={"input": prompt, "stream": False}, headers=headers
                )
                resp.raise_for_status()
                body = resp.json()
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
        raise NotImplementedError("resolver lands in Phase 3")


_KEYWORDS: dict[Category, tuple[str, ...]] = {
    Category.billing: ("charge", "invoice", "refund", "subscription", "payment", "license"),
    Category.hr: ("leave", "pto", "vacation", "benefit", "payroll", "paycheck", "parental"),
    Category.support: ("vpn", "password", "login", "laptop", "install", "email", "network"),
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
                    confidence=min(0.95, 0.6 + 0.1 * len(hits)),
                    rationale=f"matched keywords: {', '.join(hits)}",
                )
        return Classification(
            category=Category.support,
            confidence=0.3,
            rationale="no keyword match; defaulting to support with low confidence",
        )

    async def invoke_resolver(self, req: ResolverInput) -> ResolverOutput:
        raise NotImplementedError("resolver lands in Phase 3")
