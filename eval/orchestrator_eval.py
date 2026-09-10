"""Code-based end-to-end orchestrator evaluation (README §7, Phase 4).

    uv run python -m eval.orchestrator_eval
    uv run python -m eval.orchestrator_eval --gate --min-outcome-accuracy 0.90

Runs each scenario through the whole graph (`classify -> route -> resolve |
escalate_low_confidence -> finalize`) and reports outcome accuracy (outcome +
escalation reason), a hard billing sub-gate, low_confidence recall, citation
validity for answered rows, and that every escalated run wrote a matching
`EscalationRecord`. Writes a JSON report to `.local/eval/`. With `--gate`, exits
non-zero on any failed threshold.

Mode comes from `HELPDESK_{CLASSIFIER,RESOLVER}_MODE` / `HELPDESK_AGENT_MODE`
(no `--mode` flag — same as the classifier / resolver evals). Escalation records
are written to a throwaway store so runs stay isolated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from rich.console import Console
from rich.table import Table

from helpdesk.agent_gateway import build_graph_invoker
from helpdesk.agents.orchestrator.graph import build_graph, run_graph
from helpdesk.config import get_settings
from helpdesk.contracts import ClassifierInput
from helpdesk.escalation import get_escalation_store
from helpdesk.logging import configure_logging
from helpdesk.search.chunking import iter_chunks

console = Console()
_DOCS_DIR = Path("docs")


def _kb_doc_ids() -> set[str]:
    """Every doc_id in the KB — pure, filesystem only (no network)."""
    return {c.doc_id for c in iter_chunks(_DOCS_DIR)}


@dataclass
class RowResult:
    request_id: str
    message_text: str
    expected_outcome: str
    predicted_outcome: str
    expected_reason: str | None
    predicted_reason: str | None
    expected_category: str | None
    predicted_category: str | None
    n_citations: int
    cited_doc_ids: list[str]
    citations_valid: bool
    correct_outcome: bool
    escalation_recorded: bool
    latency_ms: float


@dataclass
class Report:
    dataset: str
    classifier_model: str
    resolver_model: str
    n: int
    outcome_accuracy: float
    billing_ok: bool
    billing_n: int
    low_confidence: dict[str, float]
    citation_validity_rate: float
    escalation_record_rate: float
    mean_latency_ms: float
    rows: list[dict] = field(default_factory=list)


async def _run_all(rows: list[dict], concurrency: int) -> list[RowResult]:
    # Throwaway escalation store so eval runs don't touch .local/escalations.json.
    tmp = Path(tempfile.mkdtemp(prefix="orch-eval-")) / "escalations.json"
    settings = get_settings().model_copy(update={"escalation_store_path": str(tmp)})
    invoker = build_graph_invoker(settings)
    graph = build_graph(settings, invoker=invoker)
    store = get_escalation_store(settings)
    kb_ids = _kb_doc_ids()
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
            res = await run_graph(graph, req)
            ms = (time.perf_counter() - start) * 1000

        expected_outcome = row["expected_outcome"]
        expected_reason = row.get("expected_escalation_reason")
        expected_category = row.get("expected_category")
        cited = [c.doc_id for c in res.citations]

        correct = res.outcome == expected_outcome and res.escalation_reason == expected_reason
        if expected_category is not None:
            correct = correct and res.category == expected_category

        recorded = False
        if res.outcome == "escalated":
            rec = await store.get(req.request_id)
            recorded = rec is not None and rec.escalation_reason == res.escalation_reason

        return RowResult(
            request_id=req.request_id,
            message_text=req.message_text,
            expected_outcome=expected_outcome,
            predicted_outcome=res.outcome,
            expected_reason=expected_reason,
            predicted_reason=res.escalation_reason,
            expected_category=expected_category,
            predicted_category=res.category,
            n_citations=len(cited),
            cited_doc_ids=cited,
            citations_valid=all(d in kb_ids for d in cited),
            correct_outcome=correct,
            escalation_recorded=recorded,
            latency_ms=ms,
        )

    try:
        return list(await asyncio.gather(*(one(r) for r in rows)))
    finally:
        await invoker.aclose()


def build_report(dataset: str, results: list[RowResult]) -> Report:
    settings = get_settings()
    n = len(results)

    billing = [r for r in results if r.expected_category == "billing"]
    billing_ok = all(
        r.predicted_outcome == "escalated" and r.predicted_reason == "policy_escalation"
        for r in billing
    )

    lc_gold = [r for r in results if r.expected_reason == "low_confidence"]
    lc_hit = [r for r in lc_gold if r.predicted_reason == "low_confidence"]
    lc_pred = [r for r in results if r.predicted_reason == "low_confidence"]
    lc_tp = [r for r in lc_pred if r.expected_reason == "low_confidence"]

    answered = [r for r in results if r.predicted_outcome == "answered"]
    escalated = [r for r in results if r.predicted_outcome == "escalated"]

    return Report(
        dataset=dataset,
        classifier_model=settings.classifier_model,
        resolver_model=settings.resolver_model,
        n=n,
        outcome_accuracy=sum(r.correct_outcome for r in results) / n if n else 0.0,
        billing_ok=billing_ok,
        billing_n=len(billing),
        low_confidence={
            "recall": len(lc_hit) / len(lc_gold) if lc_gold else 1.0,
            "precision": len(lc_tp) / len(lc_pred) if lc_pred else 1.0,
            "gold": float(len(lc_gold)),
        },
        citation_validity_rate=(
            sum(r.citations_valid for r in answered) / len(answered) if answered else 1.0
        ),
        escalation_record_rate=(
            sum(r.escalation_recorded for r in escalated) / len(escalated) if escalated else 1.0
        ),
        mean_latency_ms=mean(r.latency_ms for r in results) if results else 0.0,
        rows=[asdict(r) for r in results],
    )


def render(report: Report) -> None:
    console.print(
        f"\n[bold]orchestrator eval[/bold]  "
        f"classifier=[cyan]{report.classifier_model}[/cyan] "
        f"resolver=[cyan]{report.resolver_model}[/cyan]  n={report.n}"
    )
    console.print(
        f"outcome accuracy [bold]{report.outcome_accuracy:.1%}[/bold]   "
        f"mean latency {report.mean_latency_ms:.0f} ms"
    )
    console.print(
        f"billing sub-gate ({report.billing_n} rows → escalated/policy_escalation): "
        + ("[green]PASS[/green]" if report.billing_ok else "[red]FAIL[/red]")
    )
    lc = report.low_confidence
    console.print(
        f"low_confidence: recall [bold]{lc['recall']:.1%}[/bold] "
        f"precision [bold]{lc['precision']:.1%}[/bold] ({int(lc['gold'])} gold)"
    )
    console.print(
        f"citation validity (answered rows): {report.citation_validity_rate:.1%}   "
        f"escalation records written: {report.escalation_record_rate:.1%}"
    )

    wrong = [r for r in report.rows if not r["correct_outcome"]]
    if wrong:
        wt = Table(title="outcome errors", title_style="dim red")
        for col in ("id", "expected", "got", "message"):
            wt.add_column(col, overflow="fold", max_width=52 if col == "message" else None)
        for r in wrong:
            wt.add_row(
                r["request_id"],
                f"{r['expected_outcome']}/{r['expected_reason'] or '-'}/{r['expected_category'] or '*'}",
                f"{r['predicted_outcome']}/{r['predicted_reason'] or '-'}/{r['predicted_category'] or '-'}",
                r["message_text"],
            )
        console.print(wt)

    bad_cites = [
        r for r in report.rows if r["predicted_outcome"] == "answered" and not r["citations_valid"]
    ]
    for r in bad_cites:
        console.print(f"[red]{r['request_id']} cited unknown doc_ids:[/red] {r['cited_doc_ids']}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=Path("eval/datasets/orchestrator_scenarios.jsonl"))
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--gate", action="store_true", help="exit non-zero on any failed threshold")
    ap.add_argument("--min-outcome-accuracy", type=float, default=0.90)
    ap.add_argument("--min-lowconf-recall", type=float, default=1.0)
    ap.add_argument("--compare", type=Path, help="a prior report JSON; report outcome parity")
    ap.add_argument("--min-parity", type=float, default=0.95, help="with --gate, min row parity")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    configure_logging("DEBUG" if args.debug else "WARNING")

    rows = [json.loads(line) for line in args.dataset.read_text("utf-8").splitlines() if line.strip()]
    console.print(f"[dim]running {len(rows)} scenarios from {args.dataset}...[/dim]")

    results = await _run_all(rows, args.concurrency)
    report = build_report(str(args.dataset), results)
    render(report)

    out_dir = Path(".local/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"orchestrator_{stamp}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2), "utf-8")
    console.print(f"[dim]report -> {out_path}[/dim]")

    parity: float | None = None
    if args.compare:
        prior = {
            r["request_id"]: (r["predicted_outcome"], r["predicted_reason"])
            for r in json.loads(args.compare.read_text("utf-8"))["rows"]
        }
        shared = [r for r in report.rows if r["request_id"] in prior]
        agree = [
            r
            for r in shared
            if (r["predicted_outcome"], r["predicted_reason"]) == prior[r["request_id"]]
        ]
        parity = len(agree) / len(shared) if shared else 0.0
        console.print(
            f"[dim]outcome parity vs {args.compare.name}:[/dim] "
            f"[bold]{parity:.1%}[/bold] ({len(agree)}/{len(shared)} rows)"
        )

    if args.gate:
        failed = False
        if report.outcome_accuracy < args.min_outcome_accuracy:
            console.print(
                f"[red]GATE FAILED: outcome accuracy {report.outcome_accuracy:.1%} "
                f"< {args.min_outcome_accuracy:.0%}[/red]"
            )
            failed = True
        if not report.billing_ok:
            console.print("[red]GATE FAILED: billing sub-gate — a billing row did not escalate[/red]")
            failed = True
        if report.low_confidence["recall"] < args.min_lowconf_recall:
            console.print(
                f"[red]GATE FAILED: low_confidence recall {report.low_confidence['recall']:.1%} "
                f"< {args.min_lowconf_recall:.0%}[/red]"
            )
            failed = True
        if report.citation_validity_rate != 1.0:
            console.print("[red]GATE FAILED: an answered row cited a doc_id not in the KB[/red]")
            failed = True
        if report.escalation_record_rate != 1.0:
            console.print("[red]GATE FAILED: an escalated run did not write an EscalationRecord[/red]")
            failed = True
        if parity is not None and parity < args.min_parity:
            console.print(f"[red]GATE FAILED: parity {parity:.1%} < {args.min_parity:.0%}[/red]")
            failed = True
        if failed:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
