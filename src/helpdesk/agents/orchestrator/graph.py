"""The orchestrator graph — pure builder + run helper.

``classify -> route -> resolve | escalate_low_confidence -> finalize`` (README §2).
No network or model calls at import time; ``build_graph`` takes a pre-built
:class:`AgentInvoker` whose lifetime the caller owns (mirrors
``build_resolver_agent(settings, kb=...)``).

Routing (CLAUDE.md "Routing"):
- ``confidence < settings.confidence_threshold`` -> escalate ``low_confidence``,
  regardless of category.
- ``billing`` flows through ``resolve``; the invoker seam short-circuits it to
  ``policy_escalation`` before any model/search call.
- resolver not grounded -> escalate ``not_grounded``.

Every escalation is written to the escalation store in ``finalize`` — the graph's
single I/O point.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph

from helpdesk.config import Settings
from helpdesk.contracts import (
    Classification,
    ClassifierInput,
    EscalationReason,
    EscalationRecord,
    HelpdeskResult,
    ResolverInput,
    ResolverOutput,
)
from helpdesk.escalation import get_escalation_store

if TYPE_CHECKING:
    from helpdesk.agent_gateway import AgentInvoker

logger = logging.getLogger("helpdesk.orchestrator")

_RouteTarget = Literal["resolve", "escalate_low_confidence"]

# langgraph's compiled-graph generic takes four type params and its exact shape
# shifts between minor versions; the graph is an internal handle passed straight
# back to ``run_graph``, so an alias keeps call sites clean without pinning it.
CompiledGraph = Any


class GraphState(TypedDict):
    """Threaded through the graph. Only ``request`` is set at invoke time."""

    request: ClassifierInput
    classification: NotRequired[Classification]
    resolver_output: NotRequired[ResolverOutput]
    escalation_reason: NotRequired[EscalationReason]
    result: NotRequired[HelpdeskResult]


def build_graph(settings: Settings, *, invoker: AgentInvoker) -> CompiledGraph:
    """Wire and compile the orchestrator graph on ``invoker``."""

    async def classify(state: GraphState) -> dict[str, Any]:
        classification = await invoker.invoke_classifier(state["request"])
        logger.info(
            "classify %s -> %s (%.2f)",
            state["request"].request_id,
            classification.category,
            classification.confidence,
        )
        return {"classification": classification}

    async def route(_state: GraphState) -> dict[str, Any]:
        # No state change — the branch is the conditional edge below. Kept as a
        # node so it shows as its own span (README §2 diagram).
        return {}

    async def resolve(state: GraphState) -> dict[str, Any]:
        classification = state["classification"]
        req = state["request"]
        out = await invoker.invoke_resolver(
            ResolverInput(
                request_id=req.request_id,
                category=classification.category,
                message_text=req.message_text,
            )
        )
        logger.info(
            "resolve %s -> %s%s",
            req.request_id,
            out.status,
            f" ({out.escalation_reason})" if out.escalation_reason else "",
        )
        return {"resolver_output": out}

    async def escalate_low_confidence(state: GraphState) -> dict[str, Any]:
        logger.info(
            "escalate %s -> low_confidence (%.2f < %.2f)",
            state["request"].request_id,
            state["classification"].confidence,
            settings.confidence_threshold,
        )
        return {"escalation_reason": "low_confidence"}

    def route_edge(state: GraphState) -> _RouteTarget:
        """Below the confidence threshold -> escalate, regardless of category."""
        if state["classification"].confidence < settings.confidence_threshold:
            return "escalate_low_confidence"
        return "resolve"

    async def finalize(state: GraphState) -> dict[str, Any]:
        result = _to_result(state)
        if result.outcome == "escalated":
            assert result.escalation_reason is not None  # set by _to_result
            assert result.category is not None
            await get_escalation_store(settings).record(
                EscalationRecord(
                    request_id=result.request_id,
                    category=result.category,
                    escalation_reason=result.escalation_reason,
                )
            )
            logger.info(
                "escalation recorded: %s / %s / %s",
                result.request_id,
                result.category,
                result.escalation_reason,
            )
        return {"result": result}

    graph: StateGraph[GraphState, Any, Any, Any] = StateGraph(GraphState)
    # langgraph types the node param as ``_Node[Never]``; a plain
    # ``(GraphState) -> dict`` coroutine doesn't match its overloads.
    for name, fn in (
        ("classify", classify),
        ("route", route),
        ("resolve", resolve),
        ("escalate_low_confidence", escalate_low_confidence),
        ("finalize", finalize),
    ):
        graph.add_node(name, fn)  # type: ignore[arg-type]

    graph.add_edge(START, "classify")
    graph.add_edge("classify", "route")
    graph.add_conditional_edges(
        "route",
        route_edge,
        {"resolve": "resolve", "escalate_low_confidence": "escalate_low_confidence"},
    )
    graph.add_edge("resolve", "finalize")
    graph.add_edge("escalate_low_confidence", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


def _to_result(state: GraphState) -> HelpdeskResult:
    request = state["request"]
    classification = state.get("classification")
    category = classification.category if classification else None

    if state.get("escalation_reason") == "low_confidence":
        return HelpdeskResult(
            request_id=request.request_id,
            outcome="escalated",
            category=category,
            escalation_reason="low_confidence",
            classification=classification,
        )

    out = state.get("resolver_output")
    if out is None:  # defensive — the graph always sets one of the two
        raise RuntimeError(f"orchestrator reached finalize with no outcome for {request.request_id!r}")

    if out.status == "answered":
        return HelpdeskResult(
            request_id=request.request_id,
            outcome="answered",
            category=category,
            answer=out.answer,
            citations=out.citations,
            classification=classification,
        )
    return HelpdeskResult(
        request_id=request.request_id,
        outcome="escalated",
        category=category,
        escalation_reason=out.escalation_reason,
        classification=classification,
    )


async def run_graph(
    graph: CompiledGraph,
    request: ClassifierInput,
    *,
    tracer: object | None = None,
) -> HelpdeskResult:
    """Run the compiled graph for one request and return its ``HelpdeskResult``.

    ``tracer`` — an ``AzureAIOpenTelemetryTracer`` (or any LangChain callback);
    attached per the documented ``config={"callbacks": [...]}`` pattern.
    """
    config: dict[str, Any] = {"configurable": {"thread_id": request.request_id}}
    if tracer is not None:
        config["callbacks"] = [tracer]
    state = await graph.ainvoke({"request": request}, config)
    result = state["result"]
    assert isinstance(result, HelpdeskResult)  # every path through finalize sets it
    return result
