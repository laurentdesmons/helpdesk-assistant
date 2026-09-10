"""Container entrypoint for the Foundry-hosted orchestrator.

Thin: no business logic. The orchestrator is a LangGraph app, so it can't be an
MAF ``Agent`` directly — ``_GraphAgent`` is a minimal shim implementing the
``SupportsAgentRun`` protocol (``id`` / ``name`` / ``description`` + ``run``) that
``ResponsesHostServer`` needs. Each request text is parsed as a
``{request_id, user_id, message_text, channel}`` JSON object (raw text is
tolerated as ``message_text``), run through the graph, and returned as a single
assistant message carrying ``HelpdeskResult`` JSON.

    python -m helpdesk.agents.orchestrator.host      # local host on :8088

Started in the Foundry container via ``main.py`` (``HELPDESK_AGENT_ROLE=orchestrator``);
the runtime injects ``FOUNDRY_PROJECT_ENDPOINT`` +
``APPLICATIONINSIGHTS_CONNECTION_STRING``. The deployed agents' Responses
endpoints come through ``azure.yaml`` ``env:`` (``HELPDESK_AGENT_MODE=remote``).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from agent_framework import AgentResponse, AgentResponseUpdate, Content, Message
from agent_framework_foundry_hosting import ResponsesHostServer

from helpdesk.agent_gateway import build_graph_invoker
from helpdesk.agents.orchestrator.graph import build_graph, run_graph
from helpdesk.config import Settings, get_settings
from helpdesk.contracts import ClassifierInput
from helpdesk.logging import configure_logging
from helpdesk.tracing import build_tracer, configure_tracing

logger = logging.getLogger("helpdesk.orchestrator.host")


def _to_request(messages: Sequence[Message] | Message | str | None) -> ClassifierInput:
    """Coerce the inbound Responses payload into a ``ClassifierInput``."""
    if messages is None:
        text = ""
    elif isinstance(messages, str):
        text = messages
    elif isinstance(messages, Message):
        text = messages.text
    else:
        user_texts = [m.text for m in messages if getattr(m, "role", None) == "user" and m.text]
        text = user_texts[-1] if user_texts else " ".join(m.text for m in messages if m.text)

    text = text.strip()
    data: dict[str, Any] = {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            pass

    return ClassifierInput(
        request_id=str(data.get("request_id") or uuid.uuid4()),
        user_id=str(data.get("user_id") or "orchestrator"),
        message_text=str(data.get("message_text") or text or "(empty request)"),
        channel=str(data.get("channel") or "portal"),
    )


class _GraphAgent:
    """``SupportsAgentRun`` shim wrapping the compiled orchestrator graph."""

    def __init__(self, settings: Settings) -> None:
        self.id = settings.orchestrator_agent_name
        self.name = settings.orchestrator_agent_name
        self.description = "IT help desk orchestrator: classify -> route -> resolve | escalate."
        self._invoker = build_graph_invoker(settings)
        self._graph = build_graph(settings, invoker=self._invoker)
        self._tracer = build_tracer(settings)

    async def _run_once(self, messages: Sequence[Message] | Message | str | None) -> str:
        request = _to_request(messages)
        result = await run_graph(self._graph, request, tracer=self._tracer)
        logger.info("orchestrated %s -> %s", result.request_id, result.outcome)
        return result.model_dump_json()

    def run(
        self,
        messages: Sequence[Message] | Message | str | None = None,
        *,
        stream: bool = False,
        **_kwargs: Any,
    ) -> Any:
        if stream:
            return self._run_stream(messages)
        return self._run_nonstream(messages)

    async def _run_nonstream(
        self, messages: Sequence[Message] | Message | str | None
    ) -> AgentResponse[Any]:
        text = await self._run_once(messages)
        return AgentResponse(messages=[Message(role="assistant", contents=[text])])

    async def _run_stream(
        self, messages: Sequence[Message] | Message | str | None
    ) -> AsyncIterator[AgentResponseUpdate]:
        text = await self._run_once(messages)
        yield AgentResponseUpdate(role="assistant", contents=[Content("text", text=text)])


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing(settings)
    ResponsesHostServer(_GraphAgent(settings)).run()


if __name__ == "__main__":
    main()
