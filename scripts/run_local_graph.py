"""Drive the orchestrator graph locally with rich logs.

    uv run python scripts/run_local_graph.py --message "my vpn keeps dropping" --mode local
    uv run python scripts/run_local_graph.py --batch eval/datasets/orchestrator_scenarios.jsonl --mode fake
    uv run python scripts/run_local_graph.py --message "I was charged twice" --mode remote

`--mode` forces both agents to one mode; omit it to let each follow its own
`HELPDESK_{CLASSIFIER,RESOLVER}_MODE`. Thin driver: all logic is in `helpdesk`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from helpdesk.agent_gateway import build_graph_invoker
from helpdesk.agents.orchestrator.graph import build_graph, run_graph
from helpdesk.config import get_settings
from helpdesk.contracts import ClassifierInput
from helpdesk.logging import configure_logging
from helpdesk.tracing import build_tracer, configure_tracing

console = Console()


def _rows(path: Path, channel: str, user_id: str) -> list[ClassifierInput]:
    out: list[ClassifierInput] = []
    for i, line in enumerate(path.read_text("utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        out.append(
            ClassifierInput(
                request_id=data.get("request_id", f"row-{i}"),
                user_id=data.get("user_id", user_id),
                message_text=data["message_text"],
                channel=data.get("channel", channel),
            )
        )
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--message", help="a single request to orchestrate")
    g.add_argument("--batch", type=Path, help="a .jsonl file of {message_text, ...} rows")
    ap.add_argument("--channel", default="portal")
    ap.add_argument("--user-id", default="cli")
    ap.add_argument("--mode", choices=["local", "remote", "fake"], help="force both agents to one mode")
    ap.add_argument("--no-trace", action="store_true", help="don't attach the tracing callback")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    configure_logging("DEBUG" if args.debug else settings.log_level)

    tracer = None
    if not args.no_trace:
        configure_tracing(settings)
        tracer = build_tracer(settings)

    console.print(
        f"[dim]mode=[/dim]{args.mode or 'per-agent'}  "
        f"[dim]classifier=[/dim]{settings.mode_for('classifier')}  "
        f"[dim]resolver=[/dim]{settings.mode_for('resolver')}  "
        f"[dim]trace=[/dim]{'on' if tracer else 'off'}"
    )

    invoker = build_graph_invoker(settings, args.mode)
    graph = build_graph(settings, invoker=invoker)

    if args.message:
        reqs = [
            ClassifierInput(
                request_id="cli-1",
                user_id=args.user_id,
                message_text=args.message,
                channel=args.channel,
            )
        ]
    else:
        reqs = _rows(args.batch, args.channel, args.user_id)

    results: list[tuple[ClassifierInput, object, float]] = []
    try:
        for req in reqs:
            start = time.perf_counter()
            res = await run_graph(graph, req, tracer=tracer)
            results.append((req, res, (time.perf_counter() - start) * 1000))
    finally:
        await invoker.aclose()

    table = Table(show_lines=len(results) <= 5)
    table.add_column("request", overflow="fold", max_width=44)
    table.add_column("outcome")
    table.add_column("cat")
    table.add_column("reason")
    table.add_column("cites", justify="right")
    table.add_column("answer", overflow="fold", max_width=54)
    table.add_column("ms", justify="right")
    for req, res, ms in results:
        colour = {"answered": "green", "escalated": "yellow"}.get(res.outcome, "white")
        table.add_row(
            req.message_text,
            f"[{colour}]{res.outcome}[/{colour}]",
            str(res.category or "-"),
            res.escalation_reason or "-",
            str(len(res.citations)),
            res.answer or "-",
            f"{ms:.0f}",
        )
    console.print(table)

    if len(results) > 1:
        avg = sum(ms for *_, ms in results) / len(results)
        escalated = sum(1 for _, r, _ in results if r.outcome == "escalated")
        console.print(
            f"[dim]{len(results)} requests · avg {avg:.0f} ms · {escalated} escalated[/dim]"
        )


if __name__ == "__main__":
    asyncio.run(main())
