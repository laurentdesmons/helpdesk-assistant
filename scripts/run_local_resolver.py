"""Drive the resolver locally with rich logs.

    uv run python scripts/run_local_resolver.py --message "how do I fix VPN drops" --category support
    uv run python scripts/run_local_resolver.py --batch eval/datasets/resolver_labeled.jsonl
    uv run python scripts/run_local_resolver.py --message "double charge" --category billing --mode fake

Thin driver: all logic is in `helpdesk` / `agents`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from helpdesk.agent_gateway import build_invoker
from helpdesk.config import get_settings
from helpdesk.contracts import ResolverInput
from helpdesk.logging import configure_logging

console = Console()


async def _one(invoker, req: ResolverInput) -> tuple[ResolverInput, object, float]:
    start = time.perf_counter()
    result = await invoker.invoke_resolver(req)
    return req, result, (time.perf_counter() - start) * 1000


def _rows(path: Path) -> list[ResolverInput]:
    out: list[ResolverInput] = []
    for i, line in enumerate(path.read_text("utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        out.append(
            ResolverInput(
                request_id=data.get("request_id", f"row-{i}"),
                category=data["category"],
                message_text=data["message_text"],
            )
        )
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--message", help="a single request to resolve")
    g.add_argument("--batch", type=Path, help="a .jsonl file of {category, message_text, ...} rows")
    ap.add_argument("--category", choices=["support", "hr", "billing"], help="required with --message")
    ap.add_argument("--mode", choices=["local", "fake"], help="override HELPDESK_AGENT_MODE")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.message and not args.category:
        ap.error("--category is required with --message")

    settings = get_settings()
    configure_logging("DEBUG" if args.debug else settings.log_level)

    mode = args.mode or settings.mode_for("resolver")
    console.print(f"[dim]mode=[/dim]{mode}  [dim]model=[/dim]{settings.resolver_model}")
    invoker = build_invoker(settings, mode)

    if args.message:
        reqs = [
            ResolverInput(request_id="cli-1", category=args.category, message_text=args.message)
        ]
    else:
        reqs = _rows(args.batch)

    try:
        results = [await _one(invoker, r) for r in reqs]
    finally:
        await invoker.aclose()

    table = Table(show_lines=len(results) <= 5)
    table.add_column("request", overflow="fold", max_width=48)
    table.add_column("cat")
    table.add_column("status")
    table.add_column("reason")
    table.add_column("cites", justify="right")
    table.add_column("answer", overflow="fold", max_width=60)
    table.add_column("ms", justify="right")
    for req, res, ms in results:
        colour = {"answered": "green", "escalated": "yellow"}.get(res.status, "white")
        table.add_row(
            req.message_text,
            str(req.category),
            f"[{colour}]{res.status}[/{colour}]",
            res.escalation_reason or "-",
            str(len(res.citations)),
            res.answer or "-",
            f"{ms:.0f}",
        )
    console.print(table)

    if len(results) > 1:
        avg = sum(ms for *_, ms in results) / len(results)
        escalated = sum(1 for _, r, _ in results if r.status == "escalated")
        not_grounded = sum(1 for _, r, _ in results if r.escalation_reason == "not_grounded")
        console.print(
            f"[dim]{len(results)} requests · avg {avg:.0f} ms · "
            f"{escalated} escalated ({not_grounded} not_grounded)[/dim]"
        )


if __name__ == "__main__":
    asyncio.run(main())
