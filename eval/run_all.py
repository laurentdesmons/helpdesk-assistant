"""Consolidated deploy gate — classifier + resolver + orchestrator evals (Phase 4.5).

    uv run python -m eval.run_all              # pre-deploy: local, live models
    uv run python -m eval.run_all --remote     # post-deploy: parity pass vs the local reports

One command, one exit code. Shells out to each suite's own ``--gate`` mode
(`eval.classifier_eval`, `eval.resolver_eval`, `eval.orchestrator_eval`), streams
their rich output, then prints a combined PASS/FAIL summary and writes a merged
report to ``.local/eval/run_all_<stamp>.json``. Exits non-zero if any suite fails.

This runner computes no metrics of its own — it only orchestrates the suites and
reads back the JSON reports they already write. Thresholds mirror CLAUDE.md
"Deploy gate"; ``--remote`` sets ``HELPDESK_{CLASSIFIER,RESOLVER}_MODE=remote``
and compares each suite against its newest prior local report.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

_EVAL_DIR = Path(".local/eval")


@dataclass(frozen=True)
class Suite:
    name: str
    module: str
    report_prefix: str
    headline_flag: str  # the suite's own --min-* accuracy flag
    headline_key: str  # the field in the suite's JSON report
    local_default: float
    remote_default: float
    cli_override: str  # the run_all flag that overrides the threshold


SUITES: tuple[Suite, ...] = (
    Suite(
        name="classifier",
        module="eval.classifier_eval",
        report_prefix="classifier",
        headline_flag="--min-accuracy",
        headline_key="accuracy",
        local_default=0.94,
        remote_default=0.90,
        cli_override="min_classifier_accuracy",
    ),
    Suite(
        name="resolver",
        module="eval.resolver_eval",
        report_prefix="resolver",
        headline_flag="--min-routing-accuracy",
        headline_key="routing_accuracy",
        local_default=0.90,
        remote_default=0.85,
        cli_override="min_resolver_routing_accuracy",
    ),
    Suite(
        name="orchestrator",
        module="eval.orchestrator_eval",
        report_prefix="orchestrator",
        headline_flag="--min-outcome-accuracy",
        headline_key="outcome_accuracy",
        local_default=0.90,
        remote_default=0.85,
        cli_override="min_orchestrator_outcome_accuracy",
    ),
)


@dataclass
class SuiteResult:
    name: str
    module: str
    argv: list[str]
    returncode: int
    duration_s: float
    report_path: str | None = None
    headline_metric: float | None = None
    n: int | None = None

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass
class RunReport:
    mode: str
    started_at: str
    passed: bool
    suites: list[dict] = field(default_factory=list)


def _newest_report(prefix: str) -> Path | None:
    return max(
        _EVAL_DIR.glob(f"{prefix}_*.json"),
        key=os.path.getmtime,
        default=None,
    )


def _threshold(suite: Suite, args: argparse.Namespace, remote: bool) -> float:
    override = getattr(args, suite.cli_override, None)
    if override is not None:
        return float(override)
    return suite.remote_default if remote else suite.local_default


def _run_suite(suite: Suite, args: argparse.Namespace) -> SuiteResult:
    remote: bool = args.remote
    argv = ["--gate", suite.headline_flag, f"{_threshold(suite, args, remote):g}"]

    if remote:
        prior = _newest_report(suite.report_prefix)
        if prior is not None:
            argv += ["--compare", str(prior), "--min-parity", f"{args.min_parity:g}"]
        else:
            console.print(
                f"[yellow]{suite.name}: no prior local report in {_EVAL_DIR}/ — "
                "running --remote without a parity check.[/yellow]"
            )
    if args.debug:
        argv.append("--debug")

    env = os.environ.copy()
    if remote:
        env["HELPDESK_CLASSIFIER_MODE"] = "remote"
        env["HELPDESK_RESOLVER_MODE"] = "remote"

    cmd = [sys.executable, "-m", suite.module, *argv]
    console.rule(f"[bold]{suite.name}[/bold]  ({' '.join(cmd[2:])})")

    before = _newest_report(suite.report_prefix)
    start = time.perf_counter()
    proc = subprocess.run(cmd, env=env, check=False)  # noqa: S603 — fixed argv, our own modules
    duration = time.perf_counter() - start

    result = SuiteResult(
        name=suite.name,
        module=suite.module,
        argv=argv,
        returncode=proc.returncode,
        duration_s=round(duration, 1),
    )

    after = _newest_report(suite.report_prefix)
    if after is not None and after != before:
        result.report_path = str(after)
        try:
            data = json.loads(after.read_text("utf-8"))
            result.headline_metric = data.get(suite.headline_key)
            result.n = data.get("n") or data.get("n_scored")
        except (json.JSONDecodeError, OSError) as exc:
            console.print(f"[yellow]{suite.name}: could not read report {after}: {exc}[/yellow]")
    elif proc.returncode == 0:
        # Passed but wrote no fresh report — treat as a runner failure so the gate
        # never green-lights on a suite that didn't actually execute.
        console.print(f"[red]{suite.name}: exited 0 but wrote no report — failing the gate.[/red]")
        result.returncode = 1

    return result


def _render_summary(results: list[SuiteResult], mode: str) -> None:
    table = Table(title=f"run_all summary  (mode: {mode})", title_style="bold")
    for col in ("suite", "n", "metric", "duration", "verdict"):
        table.add_column(col, justify="right" if col != "suite" else "left")
    for r in results:
        metric = f"{r.headline_metric:.1%}" if r.headline_metric is not None else "-"
        verdict = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        table.add_row(r.name, str(r.n or "-"), metric, f"{r.duration_s:g}s", verdict)
    console.print(table)


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolidated classifier + resolver + orchestrator eval gate.")
    ap.add_argument(
        "--only",
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        default=None,
        help="comma-separated subset of: classifier,resolver,orchestrator",
    )
    ap.add_argument(
        "--remote",
        action="store_true",
        help="run classifier+resolver against the deployed agents (relaxed thresholds) "
        "and compare each suite against its newest local report",
    )
    ap.add_argument("--min-classifier-accuracy", type=float, default=None)
    ap.add_argument("--min-resolver-routing-accuracy", type=float, default=None)
    ap.add_argument("--min-orchestrator-outcome-accuracy", type=float, default=None)
    ap.add_argument("--min-parity", type=float, default=0.95, help="--remote row-parity floor")
    ap.add_argument("--debug", action="store_true", help="pass --debug to each suite")
    args = ap.parse_args()

    suites = list(SUITES)
    if args.only:
        known = {s.name for s in SUITES}
        unknown = [n for n in args.only if n not in known]
        if unknown:
            console.print(f"[red]unknown suite(s): {', '.join(unknown)} (known: {', '.join(known)})[/red]")
            return 2
        suites = [s for s in SUITES if s.name in args.only]

    mode = "remote" if args.remote else "local"
    console.print(
        f"[dim]run_all[/dim]  mode=[cyan]{mode}[/cyan]  suites={', '.join(s.name for s in suites)}"
    )

    results = [_run_suite(s, args) for s in suites]
    _render_summary(results, mode)

    passed = all(r.passed for r in results)

    _EVAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = RunReport(
        mode=mode,
        started_at=stamp,
        passed=passed,
        suites=[asdict(r) for r in results],
    )
    out_path = _EVAL_DIR / f"run_all_{stamp}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2), "utf-8")
    console.print(f"[dim]report -> {out_path}[/dim]")

    if not passed:
        failed = ", ".join(r.name for r in results if not r.passed)
        console.print(f"[red]GATE FAILED: {failed}[/red]")
        return 1
    console.print("[green]GATE PASSED[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
