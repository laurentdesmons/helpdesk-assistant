"""OpenTelemetry tracing for the LangGraph orchestrator (Phase 4).

``langchain-azure-ai``'s :class:`AzureAIOpenTelemetryTracer` is a LangChain
callback handler. Attached to a graph run it emits GenAI-semantic-convention
spans (``invoke_agent`` per node, ``chat`` / ``execute_tool`` underneath) and,
given an App Insights connection string, exports them to Azure Monitor.

Two functions, both pure (no work at import):

- ``configure_tracing(settings)`` — make sure a real OTel ``TracerProvider`` is
  installed, so ``opentelemetry.propagate.inject`` produces a ``traceparent`` on
  the deployed-agent HTTP calls and log lines carry ``trace=``. Skipped when an
  App Insights connection string is set — the tracer configures Azure Monitor
  (and the provider) itself.
- ``build_tracer(settings)`` — the callback handler, or ``None`` when tracing is
  disabled.

Trace-context propagation across the orchestrator -> agent endpoint calls is
*verified*, not assumed — see the deploy iteration's
``scripts/verify_trace_propagation.py`` (README §2).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from helpdesk.config import Settings

if TYPE_CHECKING:
    from langchain_azure_ai.callbacks.tracers import AzureAIOpenTelemetryTracer

logger = logging.getLogger("helpdesk.tracing")


def configure_tracing(settings: Settings) -> None:
    """Install a real ``TracerProvider`` if one isn't set yet.

    Idempotent. No-op when tracing is disabled, or when an App Insights
    connection string is present (``build_tracer``'s ``AzureAIOpenTelemetryTracer``
    configures Azure Monitor + the provider in that case).
    """
    if not settings.tracing_enabled:
        return
    if settings.applicationinsights_connection_string:
        return

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return  # already configured (by us, a prior call, or the host)

    trace.set_tracer_provider(
        TracerProvider(sampler=ParentBased(TraceIdRatioBased(settings.trace_sampling_ratio)))
    )
    logger.debug(
        "installed local TracerProvider (sampling ratio %.2f, no span export)",
        settings.trace_sampling_ratio,
    )


def build_tracer(settings: Settings) -> AzureAIOpenTelemetryTracer | None:
    """The graph's tracing callback, or ``None`` when ``tracing_enabled`` is off.

    With ``applicationinsights_connection_string`` set the tracer auto-configures
    Azure Monitor export; without it the tracer still emits spans on the local
    provider (``auto_configure_azure_monitor=False``) — enough for ``trace=`` in
    logs and ``traceparent`` injection, without a network dependency.
    """
    if not settings.tracing_enabled:
        return None

    from langchain_azure_ai.callbacks.tracers import AzureAIOpenTelemetryTracer

    kwargs: dict[str, object] = {
        "name": settings.orchestrator_agent_name,
        "agent_id": settings.orchestrator_agent_name,
        "enable_content_recording": settings.trace_content_recording,
        "trace_all_langgraph_nodes": True,
    }
    conn = settings.applicationinsights_connection_string
    if conn:
        kwargs["connection_string"] = conn
    else:
        kwargs["auto_configure_azure_monitor"] = False

    return AzureAIOpenTelemetryTracer(**kwargs)
