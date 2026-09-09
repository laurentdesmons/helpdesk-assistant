"""Drive the classifier locally with rich logs.

    uv run python scripts/run_local_classifier.py --message "I was charged twice"
    uv run python scripts/run_local_classifier.py --batch eval/datasets/classifier_labeled.jsonl
    uv run python scripts/run_local_classifier.py --message "vpn down" --mode fake

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
from helpdesk.contracts import ClassifierInput
from helpdesk.logging import configure_logging

console = Console()


async def _one(invoker, req: ClassifierInput) -> tuple[ClassifierInput, object, float]:
    start = time.perf_counter()
    result = await invoker.invoke_classifier(req)
    return req, result, (time.perf_counter() - start) * 1000


def _rows(path: Path) -> list[ClassifierInput]:
    out: list[ClassifierInput] = []
    for i, line in enumerate(path.read_text("utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        out.append(
            ClassifierInput(
                request_id=data.get("request_id", f"row-{i}"),
                user_id=data.get("user_id", "batch"),
                message_text=data["message_text"],
                channel=data.get("channel", "portal"),
            )
        )
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--message", help="a single request to classify")
    g.add_argument("--batch", type=Path, help="a .jsonl file of {message_text, ...} rows")
    ap.add_argument("--channel", default="portal")
    ap.add_argument("--mode", choices=["local", "fake"], help="override HELPDESK_AGENT_MODE")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    configure_logging("DEBUG" if args.debug else settings.log_level)

    mode = args.mode or settings.mode_for("classifier")
    console.print(f"[dim]mode=[/dim]{mode}  [dim]model=[/dim]{settings.classifier_model}")
    invoker = build_invoker(settings, mode)

    if args.message:
        reqs = [
            ClassifierInput(
                request_id="cli-1", user_id="cli", message_text=args.message, channel=args.channel
            )
        ]
    else:
        reqs = _rows(args.batch)

    results = [await _one(invoker, r) for r in reqs]

    table = Table(show_lines=len(results) <= 5)
    table.add_column("request", overflow="fold", max_width=60)
    table.add_column("category")
    table.add_column("conf", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("rationale", overflow="fold", max_width=60)
    for req, res, ms in results:
        colour = {"billing": "yellow", "support": "cyan", "hr": "green"}.get(res.category, "white")
        table.add_row(
            req.message_text,
            f"[{colour}]{res.category}[/{colour}]",
            f"{res.confidence:.2f}",
            f"{ms:.0f}",
            res.rationale,
        )
    console.print(table)

    if len(results) > 1:
        avg = sum(ms for *_, ms in results) / len(results)
        low = sum(1 for _, r, _ in results if r.confidence < settings.confidence_threshold)
        console.print(
            f"[dim]{len(results)} requests · avg {avg:.0f} ms · "
            f"{low} below confidence_threshold ({settings.confidence_threshold})[/dim]"
        )


if __name__ == "__main__":
    asyncio.run(main())
