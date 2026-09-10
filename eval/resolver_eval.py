"""Code-based resolver evaluation (README §7).

    uv run python -m eval.resolver_eval
    uv run python -m eval.resolver_eval --gate --min-routing-accuracy 0.90

Runs the resolver over a labeled set and reports routing accuracy (answer vs.
escalate, and the escalation reason), a hard billing sub-gate, not-grounded
precision/recall, and citation validity (every cited doc_id must exist in the KB).
Writes a JSON report to `.local/eval/`. With `--gate`, exits non-zero on any
failed threshold.

No LLM judge — the resolver eval is code-based. The `--judge` flag wires
`azure-ai-evaluation` groundedness if it's installed; off by default.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from rich.console import Console
from rich.table import Table

from helpdesk.agent_gateway import build_invoker
from helpdesk.config import get_settings
from helpdesk.contracts import ResolverInput
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
    category: str
    message_text: str
    expected_status: str
    predicted_status: str
    expected_reason: str | None
    predicted_reason: str | None
    answerable: bool
    n_citations: int
    cited_doc_ids: list[str]
    citations_valid: bool
    correct_routing: bool
    expected_doc_hit: bool | None
    latency_ms: float


@dataclass
class Report:
    dataset: str
    model: str
    n: int
    routing_accuracy: float
    billing_ok: bool
    billing_n: int
    not_grounded: dict[str, float]
    citation_validity_rate: float
    answered_with_citations_rate: float
    expected_doc_hit_rate: float | None
    mean_latency_ms: float
    rows: list[dict] = field(default_factory=list)


async def _resolve_all(rows: list[dict], concurrency: int) -> list[RowResult]:
    settings = get_settings()
    invoker = build_invoker(settings, settings.mode_for("resolver"))
    kb_ids = _kb_doc_ids()
    sem = asyncio.Semaphore(concurrency)

    async def one(row: dict) -> RowResult:
        req = ResolverInput(
            request_id=row["request_id"],
            category=row["category"],
            message_text=row["message_text"],
        )
        async with sem:
            start = time.perf_counter()
            out = await invoker.invoke_resolver(req)
            ms = (time.perf_counter() - start) * 1000

        cited = [c.doc_id for c in out.citations]
        expected_status = row["expected_status"]
        expected_reason = row.get("expected_reason")
        correct_routing = out.status == expected_status and (
            expected_status != "escalated" or out.escalation_reason == expected_reason
        )
        expected_docs = set(row.get("expected_doc_ids") or [])
        doc_hit: bool | None = None
        if out.status == "answered" and expected_docs:
            doc_hit = bool(expected_docs & set(cited))

        return RowResult(
            request_id=req.request_id,
            category=str(req.category),
            message_text=req.message_text,
            expected_status=expected_status,
            predicted_status=out.status,
            expected_reason=expected_reason,
            predicted_reason=out.escalation_reason,
            answerable=bool(row.get("answerable")),
            n_citations=len(cited),
            cited_doc_ids=cited,
            citations_valid=all(d in kb_ids for d in cited),
            correct_routing=correct_routing,
            expected_doc_hit=doc_hit,
            latency_ms=ms,
        )

    try:
        return list(await asyncio.gather(*(one(r) for r in rows)))
    finally:
        await invoker.aclose()


def build_report(dataset: str, results: list[RowResult]) -> Report:
    settings = get_settings()
    n = len(results)

    billing = [r for r in results if r.category == "billing"]
    billing_ok = all(
        r.predicted_status == "escalated" and r.predicted_reason == "policy_escalation"
        for r in billing
    )

    # not-grounded detection over the non-billing rows: positive == "should escalate
    # not_grounded", i.e. answerable is false and category isn't billing.
    ng_gold = [r for r in results if not r.answerable and r.category != "billing"]
    ng_pred = [
        r
        for r in results
        if r.category != "billing"
        and r.predicted_status == "escalated"
        and r.predicted_reason == "not_grounded"
    ]
    ng_tp = [r for r in ng_pred if not r.answerable]
    ng_precision = len(ng_tp) / len(ng_pred) if ng_pred else 1.0
    ng_recall = len(ng_tp) / len(ng_gold) if ng_gold else 1.0

    answered = [r for r in results if r.predicted_status == "answered"]
    answered_with_cites = [r for r in answered if r.n_citations > 0]
    doc_hit_rows = [r for r in results if r.expected_doc_hit is not None]

    return Report(
        dataset=dataset,
        model=settings.resolver_model,
        n=n,
        routing_accuracy=sum(r.correct_routing for r in results) / n if n else 0.0,
        billing_ok=billing_ok,
        billing_n=len(billing),
        not_grounded={
            "precision": ng_precision,
            "recall": ng_recall,
            "gold": float(len(ng_gold)),
            "predicted": float(len(ng_pred)),
        },
        citation_validity_rate=(
            sum(r.citations_valid for r in answered) / len(answered) if answered else 1.0
        ),
        answered_with_citations_rate=(
            len(answered_with_cites) / len(answered) if answered else 1.0
        ),
        expected_doc_hit_rate=(
            sum(bool(r.expected_doc_hit) for r in doc_hit_rows) / len(doc_hit_rows)
            if doc_hit_rows
            else None
        ),
        mean_latency_ms=mean(r.latency_ms for r in results) if results else 0.0,
        rows=[asdict(r) for r in results],
    )


def render(report: Report) -> None:
    console.print(
        f"\n[bold]resolver eval[/bold]  model=[cyan]{report.model}[/cyan]  n={report.n}"
    )
    console.print(
        f"routing accuracy [bold]{report.routing_accuracy:.1%}[/bold]   "
        f"mean latency {report.mean_latency_ms:.0f} ms"
    )
    billing_verdict = "[green]PASS[/green]" if report.billing_ok else "[red]FAIL[/red]"
    console.print(
        f"billing sub-gate ({report.billing_n} rows → escalated/policy_escalation): {billing_verdict}"
    )
    ng = report.not_grounded
    console.print(
        f"not_grounded: precision [bold]{ng['precision']:.1%}[/bold] "
        f"recall [bold]{ng['recall']:.1%}[/bold] "
        f"({int(ng['predicted'])} predicted / {int(ng['gold'])} gold)"
    )
    console.print(
        f"citation validity (answered rows): {report.citation_validity_rate:.1%}   "
        f"answered-with-citations: {report.answered_with_citations_rate:.1%}"
    )
    if report.expected_doc_hit_rate is not None:
        console.print(f"expected-doc hit rate: {report.expected_doc_hit_rate:.1%}")

    wrong = [r for r in report.rows if not r["correct_routing"]]
    if wrong:
        wt = Table(title="routing errors", title_style="dim red")
        for col in ("id", "cat", "expected", "got", "reason", "message"):
            wt.add_column(col, overflow="fold", max_width=48 if col == "message" else None)
        for r in wrong:
            wt.add_row(
                r["request_id"],
                r["category"],
                f"{r['expected_status']}/{r['expected_reason'] or '-'}",
                f"{r['predicted_status']}/{r['predicted_reason'] or '-'}",
                "",
                r["message_text"],
            )
        console.print(wt)

    bad_cites = [r for r in report.rows if r["predicted_status"] == "answered" and not r["citations_valid"]]
    if bad_cites:
        console.print("[red]answered rows citing unknown doc_ids:[/red]")
        for r in bad_cites:
            console.print(f"  [yellow]{r['request_id']}[/yellow]: {r['cited_doc_ids']}")


def _maybe_judge(enabled: bool) -> None:
    if not enabled:
        return
    if importlib.util.find_spec("azure.ai.evaluation") is None:
        console.print(
            "[dim]--judge requested but azure-ai-evaluation is not installed "
            "(pip install -e '.[eval]'); skipping groundedness judge.[/dim]"
        )
        return
    from eval.judges import judge_model  # noqa: PLC0415 — optional path

    console.print(
        f"[dim]judge model {judge_model(get_settings())!r} available — "
        "groundedness scoring is scaffolded but not run.[/dim]"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=Path("eval/datasets/resolver_labeled.jsonl"))
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--gate", action="store_true", help="exit non-zero on any failed threshold")
    ap.add_argument("--min-routing-accuracy", type=float, default=0.90)
    ap.add_argument("--min-notgrounded-recall", type=float, default=0.85)
    ap.add_argument("--compare", type=Path, help="a prior report JSON; report routing parity")
    ap.add_argument("--min-parity", type=float, default=0.95, help="with --gate, min row parity")
    ap.add_argument("--judge", action="store_true", help="run the groundedness judge if available")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    configure_logging("DEBUG" if args.debug else "WARNING")

    rows = [json.loads(line) for line in args.dataset.read_text("utf-8").splitlines() if line.strip()]
    console.print(f"[dim]running {len(rows)} rows from {args.dataset}...[/dim]")

    results = await _resolve_all(rows, args.concurrency)
    report = build_report(str(args.dataset), results)
    render(report)
    _maybe_judge(args.judge)

    out_dir = Path(".local/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"resolver_{stamp}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2), "utf-8")
    console.print(f"[dim]report -> {out_path}[/dim]")

    parity: float | None = None
    if args.compare:
        prior = {
            r["request_id"]: (r["predicted_status"], r["predicted_reason"])
            for r in json.loads(args.compare.read_text("utf-8"))["rows"]
        }
        shared = [r for r in report.rows if r["request_id"] in prior]
        agree = [
            r
            for r in shared
            if (r["predicted_status"], r["predicted_reason"]) == prior[r["request_id"]]
        ]
        parity = len(agree) / len(shared) if shared else 0.0
        console.print(
            f"[dim]routing parity vs {args.compare.name}:[/dim] "
            f"[bold]{parity:.1%}[/bold] ({len(agree)}/{len(shared)} rows)"
        )

    if args.gate:
        failed = False
        if report.routing_accuracy < args.min_routing_accuracy:
            console.print(
                f"[red]GATE FAILED: routing accuracy {report.routing_accuracy:.1%} "
                f"< {args.min_routing_accuracy:.0%}[/red]"
            )
            failed = True
        if not report.billing_ok:
            console.print("[red]GATE FAILED: billing sub-gate — a billing row did not escalate[/red]")
            failed = True
        if report.not_grounded["recall"] < args.min_notgrounded_recall:
            console.print(
                f"[red]GATE FAILED: not_grounded recall {report.not_grounded['recall']:.1%} "
                f"< {args.min_notgrounded_recall:.0%}[/red]"
            )
            failed = True
        if report.citation_validity_rate != 1.0:
            console.print("[red]GATE FAILED: an answered row cited a doc_id not in the KB[/red]")
            failed = True
        if parity is not None and parity < args.min_parity:
            console.print(f"[red]GATE FAILED: parity {parity:.1%} < {args.min_parity:.0%}[/red]")
            failed = True
        if failed:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
