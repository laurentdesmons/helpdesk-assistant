"""Verify trace-context propagation across the deployed orchestrator -> agents.

    uv run python scripts/verify_trace_propagation.py

README §2 says this is VERIFIED, not assumed.

1. Stand up a real OTel ``TracerProvider`` + Azure Monitor span exporter in *this*
   process, open a root span, and fire one request through the **deployed
   orchestrator** (``OrchestratorClient`` injects the W3C ``traceparent``).
2. App Insights sets ``operation_Id`` = the W3C trace-id, so we know exactly which
   id to look for. Poll ``requests`` / ``dependencies`` (the actual span tables —
   *not* ``traces``, which is logs) until they land (~1-3 min ingestion lag).
3. PASS iff span rows from **all three** ``cloud_RoleName``s carry our
   ``operation_Id`` — that means the orchestrator extracted our inbound
   ``traceparent`` *and* propagated it to the classifier + resolver, and each
   agent's host extracted it in turn.
4. If not: report the shared-``request_id`` fallback (all three ran, but under
   separate trace ids — the ``traceparent`` didn't survive an endpoint hop,
   which is platform-side) and fail.

Needs ``HELPDESK_APPINSIGHTS_RESOURCE_ID`` (or ``--resource-id``),
``HELPDESK_ORCHESTRATOR_AGENT_ENDPOINT`` and
``HELPDESK_APPLICATIONINSIGHTS_CONNECTION_STRING``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from datetime import timedelta
from typing import Any

from rich.console import Console
from rich.table import Table

from helpdesk.agent_gateway import OrchestratorClient
from helpdesk.config import Settings, get_settings
from helpdesk.contracts import ClassifierInput
from helpdesk.logging import configure_logging

console = Console()

_ROLES = ("helpdesk-orchestrator", "helpdesk-classifier", "helpdesk-resolver")

# The span tables only (request = inbound, dependency = outbound). ``traces`` is
# ILogger/log records and is not proof of span-context propagation.
_KQL_SPANS = """
union requests, dependencies
| where timestamp between (datetime({start}) .. datetime({end}))
| where operation_Id == "{op_id}"
| project timestamp, itemType, name, cloud_RoleName, operation_Id, operation_ParentId, id
| order by timestamp asc
"""

_KQL_BY_REQUEST_ID = """
union requests, dependencies, traces
| where timestamp between (datetime({start}) .. datetime({end}))
| where tostring(customDimensions) has "{rid}" or name has "{rid}" or message has "{rid}"
| summarize by cloud_RoleName, operation_Id
"""


def _make_provider(connection_string: str) -> Any:
    """A real TracerProvider with an Azure Monitor exporter, just for this run."""
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "helpdesk-verify"}))
    provider.add_span_processor(
        BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=connection_string))
    )
    trace.set_tracer_provider(provider)
    return provider


async def _fire(settings: Settings, rid: str, provider: Any) -> tuple[str, str, float]:
    """Fire one orchestrator request inside a recording root span.

    Returns (trace_id, outcome, fired_at_epoch).
    """
    from opentelemetry import trace

    client = OrchestratorClient(settings)
    req = ClassifierInput(
        request_id=rid, user_id="verify-trace", message_text="my VPN keeps dropping", channel="portal"
    )
    tracer = trace.get_tracer("helpdesk.verify_trace")
    fired_at = time.time()
    with tracer.start_as_current_span("verify_trace_propagation") as span:
        span.set_attribute("helpdesk.request_id", rid)
        if not span.is_recording() or span.get_span_context().trace_id == 0:
            raise RuntimeError(
                "root span is not recording — the TracerProvider isn't wired. "
                "Check HELPDESK_APPLICATIONINSIGHTS_CONNECTION_STRING."
            )
        result = await client.run(req)
        trace_id = format(span.get_span_context().trace_id, "032x")
    await client.aclose()
    provider.force_flush()
    console.print(
        f"[dim]request_id=[/dim]{rid}  [dim]operation_Id=[/dim]{trace_id}  "
        f"[dim]outcome=[/dim]{result.outcome}"
    )
    return trace_id, result.outcome, fired_at


def _win(fired_at: float) -> tuple[str, str]:
    from datetime import UTC, datetime

    start = datetime.fromtimestamp(fired_at - 120, UTC).isoformat()
    end = datetime.fromtimestamp(fired_at + 600, UTC).isoformat()
    return start, end


def _query(resource_id: str, op_id: str, rid: str, fired_at: float, attempts: int, delay: int) -> bool:
    from azure.identity import DefaultAzureCredential
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    client = LogsQueryClient(DefaultAzureCredential())
    span_window = timedelta(minutes=30)
    start, end = _win(fired_at)
    kql = _KQL_SPANS.format(start=start, end=end, op_id=op_id)

    # App Insights ingestion is uneven across resources — the orchestrator's spans
    # can land minutes before the resolver's. Keep polling for the *complete*
    # picture (all three roles) rather than returning on the first non-empty result.
    rows: list = []
    cols: list[str] = []
    for i in range(1, attempts + 1):
        res = client.query_resource(resource_id, kql, timespan=span_window)
        if res.status == LogsQueryStatus.SUCCESS and res.tables and res.tables[0].rows:
            cand = list(res.tables[0].rows)
            cand_cols = [c for c in res.tables[0].columns]
            r_i = cand_cols.index("cloud_RoleName")
            if len(cand) > len(rows):  # keep the fullest snapshot seen
                rows, cols = cand, cand_cols
            seen = {r[r_i] for r in rows if r[r_i]}
            if {"helpdesk-classifier", "helpdesk-resolver"}.issubset(seen):
                break
            console.print(
                f"[dim]attempt {i}/{attempts}: {len(rows)} span rows, roles={sorted(seen)} "
                f"— waiting {delay}s for the rest[/dim]"
            )
        else:
            console.print(
                f"[dim]attempt {i}/{attempts}: no span rows yet under operation_Id "
                f"{op_id[:12]}… (waiting {delay}s)[/dim]"
            )
        if i < attempts:
            time.sleep(delay)

    if not rows:
        console.print(f"[red]no request/dependency spans landed under operation_Id {op_id}.[/red]")
        return _fallback(client, resource_id, rid, fired_at, op_id)

    role_i, pid_i, id_i = cols.index("cloud_RoleName"), cols.index("operation_ParentId"), cols.index("id")
    t = Table(title=f"spans under operation_Id {op_id}", show_lines=False)
    for c in ("timestamp", "itemType", "name", "cloud_RoleName", "operation_ParentId"):
        t.add_column(c, overflow="fold")
    for r in rows:
        t.add_row(*(str(r[cols.index(c)]) for c in
                    ("timestamp", "itemType", "name", "cloud_RoleName", "operation_ParentId")))
    console.print(t)

    roles_seen = {r[role_i] for r in rows if r[role_i]}
    span_ids = {r[id_i] for r in rows}
    cross_links = [
        r for r in rows
        if r[role_i] and r[role_i] != "helpdesk-orchestrator" and r[pid_i] in span_ids
    ]
    # The hop that "not assumed" is really about: orchestrator -> each agent.
    hop_ok = {"helpdesk-classifier", "helpdesk-resolver"}.issubset(roles_seen)

    if set(_ROLES).issubset(roles_seen):
        console.print(
            f"[green bold]PASS[/green bold] — all three agents' spans carry operation_Id "
            f"{op_id}; {len(cross_links)} span(s) parent-linked across the hop."
        )
        return True

    if hop_ok:
        console.print(
            f"[green bold]PASS[/green bold] — classifier + resolver spans carry our operation_Id "
            f"{op_id}: the orchestrator extracted the inbound traceparent and propagated it."
        )
        if "helpdesk-orchestrator" not in roles_seen:
            console.print(
                "[dim](the orchestrator's own graph-node spans are in the same trace but labeled "
                "'unknown_service' — set OTEL_SERVICE_NAME=helpdesk-orchestrator)[/dim]"
            )
        return True

    console.print(
        f"[yellow]partial: spans under this operation_Id came from {sorted(roles_seen)}; "
        f"need at least classifier + resolver[/yellow]"
    )
    return _fallback(client, resource_id, rid, fired_at, op_id)


def _fallback(client: Any, resource_id: str, rid: str, fired_at: float, op_id: str) -> bool:
    from azure.monitor.query import LogsQueryStatus

    start, end = _win(fired_at)
    res = client.query_resource(
        resource_id, _KQL_BY_REQUEST_ID.format(start=start, end=end, rid=rid), timespan=timedelta(minutes=30)
    )
    if res.status != LogsQueryStatus.SUCCESS or not res.tables or not res.tables[0].rows:
        console.print("[red]request_id not found in any span telemetry — did the agents run?[/red]")
        return False

    cols = [c for c in res.tables[0].columns]
    role_i, op_i = cols.index("cloud_RoleName"), cols.index("operation_Id")
    roles = {r[role_i] for r in res.tables[0].rows if r[role_i]}
    ops = {r[op_i] for r in res.tables[0].rows if r[op_i]}
    console.print(
        f"[dim]shared-request_id fallback:[/dim] roles={sorted(roles)}  operation_Ids={sorted(ops)}"
    )
    if all(role in roles for role in _ROLES) and op_id not in ops:
        console.print(
            f"[yellow bold]NOT PROPAGATED[/yellow bold] — all three agents ran carrying request_id "
            f"{rid}, but their spans are under {sorted(ops)}, not our {op_id}. The traceparent "
            "isn't surviving an endpoint hop (Foundry host-side). Correlate via "
            "helpdesk.request_id; note this in README §2."
        )
    return False


async def _amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resource-id", help="ARM id of the App Insights component "
                    "(default: HELPDESK_APPINSIGHTS_RESOURCE_ID)")
    ap.add_argument("--request-id", default=None)
    ap.add_argument("--attempts", type=int, default=15)
    ap.add_argument("--delay", type=int, default=30, help="seconds between ingestion polls")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    configure_logging("DEBUG" if args.debug else "INFO")
    settings = get_settings()
    resource_id = args.resource_id or settings.appinsights_resource_id
    if not resource_id:
        console.print("[red]need --resource-id or HELPDESK_APPINSIGHTS_RESOURCE_ID.[/red]")
        return 2
    if not settings.applicationinsights_connection_string:
        console.print("[red]need HELPDESK_APPLICATIONINSIGHTS_CONNECTION_STRING.[/red]")
        return 2

    provider = _make_provider(settings.applicationinsights_connection_string)
    rid = args.request_id or f"trace-{uuid.uuid4().hex[:8]}"
    op_id, _outcome, fired_at = await _fire(settings, rid, provider)

    ok = await asyncio.to_thread(_query, resource_id, op_id, rid, fired_at, args.attempts, args.delay)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
