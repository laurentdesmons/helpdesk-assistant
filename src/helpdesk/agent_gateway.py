"""The local-vs-deployed seam.

The LangGraph orchestrator (Phase 4) never imports an agent directly. It asks
``build_invoker(settings, mode)`` for an :class:`AgentInvoker` and calls
``invoke_classifier`` / ``invoke_resolver`` on it. The graph topology is identical
in every mode; only the invoker changes:

- ``local``  — classifier & resolver run in-process as MAF ``Agent`` objects.
- ``remote`` — call the deployed Foundry agents (via ``langchain-azure-ai``).
- ``fake``   — deterministic keyword stubs for hermetic tests.

Phase 1 wires the classifier for ``local`` and ``fake``. ``remote`` and the
resolver land in later phases.
"""

from __future__ import annotations

from typing import Protocol

from helpdesk.config import AgentMode, Settings
from helpdesk.contracts import (
    Category,
    Classification,
    ClassifierInput,
    ResolverInput,
    ResolverOutput,
)


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


class RemoteInvoker:
    """Calls the deployed Foundry agents. Completed in the Phase 1 deploy step."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def invoke_classifier(self, req: ClassifierInput) -> Classification:
        raise NotImplementedError("remote classifier is wired in the Phase 1 deploy step")

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
