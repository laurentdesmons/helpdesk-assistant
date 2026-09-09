"""Code-based classifier evaluation (README §7).

    uv run python -m eval.classifier_eval
    uv run python -m eval.classifier_eval --dataset eval/datasets/classifier_labeled.jsonl --gate

Runs the classifier over a labeled set and reports accuracy, per-class
precision/recall/F1, a confusion matrix, and a confidence analysis that drives
`confidence_threshold` tuning. Writes a JSON report to `.local/eval/`. With
`--gate`, exits non-zero when accuracy is below `--min-accuracy`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from rich.console import Console
from rich.table import Table

from helpdesk.agent_gateway import build_invoker
from helpdesk.config import get_settings
from helpdesk.contracts import Category, ClassifierInput
from helpdesk.logging import configure_logging

console = Console()
CATEGORIES = [c.value for c in Category]


@dataclass
class RowResult:
    request_id: str
    message_text: str
    expected: str
    predicted: str
    confidence: float
    correct: bool
    ambiguous: bool
    latency_ms: float


@dataclass
class Report:
    dataset: str
    model: str
    n_scored: int
    n_ambiguous: int
    accuracy: float
    macro_f1: float
    per_class: dict[str, dict[str, float]]
    confusion: dict[str, dict[str, int]]
    confidence: dict[str, float]
    ambiguous_below_threshold: int
    confidence_threshold: float
    mean_latency_ms: float
    rows: list[dict] = field(default_factory=list)


async def _classify_all(rows: list[dict], concurrency: int) -> list[RowResult]:
    settings = get_settings()
    invoker = build_invoker(settings, settings.mode_for("classifier"))
    sem = asyncio.Semaphore(concurrency)

    async def one(row: dict) -> RowResult:
        req = ClassifierInput(
            request_id=row["request_id"],
            user_id="eval",
            message_text=row["message_text"],
            channel=row.get("channel", "portal"),
        )
        async with sem:
            start = time.perf_counter()
            c = await invoker.invoke_classifier(req)
            ms = (time.perf_counter() - start) * 1000
        return RowResult(
            request_id=req.request_id,
            message_text=req.message_text,
            expected=row["expected_category"],
            predicted=c.category,
            confidence=c.confidence,
            correct=c.category == row["expected_category"],
            ambiguous=bool(row.get("ambiguous")),
            latency_ms=ms,
        )

    return await asyncio.gather(*(one(r) for r in rows))


def _metrics(scored: list[RowResult]) -> tuple[dict, dict, float]:
    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    confusion = {e: {p: 0 for p in CATEGORIES} for e in CATEGORIES}
    for r in scored:
        confusion[r.expected][r.predicted] += 1
        if r.correct:
            tp[r.expected] += 1
        else:
            fp[r.predicted] += 1
            fn[r.expected] += 1

    per_class: dict[str, dict[str, float]] = {}
    f1s: list[float] = []
    for c in CATEGORIES:
        prec = tp[c] / (tp[c] + fp[c]) if tp[c] + fp[c] else 0.0
        rec = tp[c] / (tp[c] + fn[c]) if tp[c] + fn[c] else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per_class[c] = {"precision": prec, "recall": rec, "f1": f1, "support": tp[c] + fn[c]}
        f1s.append(f1)
    return per_class, confusion, mean(f1s)


def build_report(dataset: str, results: list[RowResult]) -> Report:
    settings = get_settings()
    scored = [r for r in results if not r.ambiguous]
    ambiguous = [r for r in results if r.ambiguous]
    per_class, confusion, macro_f1 = _metrics(scored)

    correct_conf = [r.confidence for r in scored if r.correct]
    wrong_conf = [r.confidence for r in scored if not r.correct]

    return Report(
        dataset=dataset,
        model=settings.classifier_model,
        n_scored=len(scored),
        n_ambiguous=len(ambiguous),
        accuracy=sum(r.correct for r in scored) / len(scored) if scored else 0.0,
        macro_f1=macro_f1,
        per_class=per_class,
        confusion=confusion,
        confidence={
            "mean_correct": mean(correct_conf) if correct_conf else 0.0,
            "mean_incorrect": mean(wrong_conf) if wrong_conf else 0.0,
            "min_correct": min(correct_conf) if correct_conf else 0.0,
            "max_incorrect": max(wrong_conf) if wrong_conf else 0.0,
        },
        ambiguous_below_threshold=sum(r.confidence < settings.confidence_threshold for r in ambiguous),
        confidence_threshold=settings.confidence_threshold,
        mean_latency_ms=mean(r.latency_ms for r in results) if results else 0.0,
        rows=[asdict(r) for r in results],
    )


def render(report: Report) -> None:
    console.print(
        f"\n[bold]classifier eval[/bold]  model=[cyan]{report.model}[/cyan]  "
        f"n={report.n_scored} (+{report.n_ambiguous} ambiguous)"
    )
    console.print(
        f"accuracy [bold]{report.accuracy:.1%}[/bold]   macro-F1 {report.macro_f1:.3f}   "
        f"mean latency {report.mean_latency_ms:.0f} ms"
    )

    pc = Table(title="per class", title_style="dim")
    for col in ("category", "precision", "recall", "f1", "support"):
        pc.add_column(col, justify="right" if col != "category" else "left")
    for cat, m in report.per_class.items():
        pc.add_row(
            cat, f"{m['precision']:.2f}", f"{m['recall']:.2f}", f"{m['f1']:.2f}", f"{int(m['support'])}"
        )
    console.print(pc)

    cm = Table(title="confusion  (row = expected, col = predicted)", title_style="dim")
    cm.add_column("exp \\ pred")
    for c in CATEGORIES:
        cm.add_column(c, justify="right")
    for exp in CATEGORIES:
        cm.add_row(
            exp,
            *[
                f"[green]{report.confusion[exp][p]}[/green]" if p == exp else str(report.confusion[exp][p])
                for p in CATEGORIES
            ],
        )
    console.print(cm)

    c = report.confidence
    console.print(
        f"[dim]confidence:[/dim] correct predictions mean [green]{c['mean_correct']:.2f}[/green] "
        f"(min {c['min_correct']:.2f})   incorrect mean [red]{c['mean_incorrect']:.2f}[/red] "
        f"(max {c['max_incorrect']:.2f})"
    )
    console.print(
        f"[dim]ambiguous rows below threshold ({report.confidence_threshold}):[/dim] "
        f"{report.ambiguous_below_threshold}/{report.n_ambiguous}"
    )

    wrong = [r for r in report.rows if not r["correct"] and not r["ambiguous"]]
    if wrong:
        wt = Table(title="misclassifications", title_style="dim red")
        for col in ("id", "expected", "predicted", "conf", "message"):
            wt.add_column(col, overflow="fold", max_width=54 if col == "message" else None)
        for r in wrong:
            wt.add_row(
                r["request_id"],
                r["expected"],
                r["predicted"],
                f"{r['confidence']:.2f}",
                r["message_text"],
            )
        console.print(wt)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=Path("eval/datasets/classifier_labeled.jsonl"))
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--gate", action="store_true", help="exit non-zero if accuracy < --min-accuracy")
    ap.add_argument("--min-accuracy", type=float, default=0.85)
    ap.add_argument(
        "--compare",
        type=Path,
        help="a prior report JSON; report per-row category parity (for local-vs-remote cutover)",
    )
    ap.add_argument("--min-parity", type=float, default=0.95, help="with --gate, min row parity")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    configure_logging("DEBUG" if args.debug else "WARNING")

    rows = [json.loads(line) for line in args.dataset.read_text("utf-8").splitlines() if line.strip()]
    console.print(f"[dim]running {len(rows)} rows from {args.dataset}...[/dim]")

    results = await _classify_all(rows, args.concurrency)
    report = build_report(str(args.dataset), results)
    render(report)

    out_dir = Path(".local/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"classifier_{stamp}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2), "utf-8")
    console.print(f"[dim]report -> {out_path}[/dim]")

    parity: float | None = None
    if args.compare:
        prior = {r["request_id"]: r["predicted"] for r in json.loads(args.compare.read_text("utf-8"))["rows"]}
        shared = [r for r in report.rows if r["request_id"] in prior]
        agree = [r for r in shared if r["predicted"] == prior[r["request_id"]]]
        parity = len(agree) / len(shared) if shared else 0.0
        console.print(
            f"[dim]category parity vs {args.compare.name}:[/dim] "
            f"[bold]{parity:.1%}[/bold] ({len(agree)}/{len(shared)} rows)"
        )
        for r in shared:
            was = prior[r["request_id"]]
            if r["predicted"] != was:
                console.print(f"  [yellow]{r['request_id']}[/yellow]: was {was}, now {r['predicted']}")

    if args.gate and report.accuracy < args.min_accuracy:
        console.print(f"[red]GATE FAILED: accuracy {report.accuracy:.1%} < {args.min_accuracy:.0%}[/red]")
        return 1
    if args.gate and parity is not None and parity < args.min_parity:
        console.print(f"[red]GATE FAILED: parity {parity:.1%} < {args.min_parity:.0%}[/red]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
