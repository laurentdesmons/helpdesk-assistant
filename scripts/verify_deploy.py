"""Post-deploy smoke test for a deployed Foundry agent.

    uv run python scripts/verify_deploy.py classifier

Forces the ``remote`` invoker (regardless of ``HELPDESK_AGENT_MODE``), sends a
handful of unambiguous requests through the deployed agent, and asserts the
responses satisfy the contract. Exits non-zero on any failure.

Thin driver: the canned cases are test data, not logic.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from rich.console import Console
from rich.table import Table

from helpdesk.agent_gateway import build_invoker
from helpdesk.config import get_settings
from helpdesk.contracts import (
    Category,
    Classification,
    ClassifierInput,
    ResolverInput,
    ResolverOutput,
)
from helpdesk.logging import configure_logging

console = Console()

# (message, channel, expected category) — deliberately unambiguous.
_CLASSIFIER_CASES: list[tuple[str, str, Category]] = [
    ("I was charged twice for my subscription this month, please refund one.", "email", Category.billing),
    ("My VPN keeps dropping every few minutes and I can't reach the server.", "teams", Category.support),
    ("How many weeks of paid parental leave am I entitled to?", "portal", Category.hr),
    ("Please reset my password, the portal says my account is locked.", "portal", Category.support),
]

# (message, category, expected status, expected escalation reason)
_RESOLVER_CASES: list[tuple[str, Category, str, str | None]] = [
    ("I was charged twice, please refund one.", Category.billing, "escalated", "policy_escalation"),
    ("My VPN keeps dropping every few minutes — what should I try?", Category.support, "answered", None),
    ("How many weeks of paid parental leave does the primary caregiver get?", Category.hr, "answered", None),
    ("Can you recommend a good lunch spot near the office?", Category.support, "escalated", "not_grounded"),
]


async def _verify_classifier() -> bool:
    settings = get_settings()
    invoker = build_invoker(settings, "remote")
    table = Table(title="classifier — remote smoke test", show_lines=True)
    for col in ("message", "expected", "got", "conf", "ms", "result"):
        table.add_column(col, overflow="fold", max_width=48 if col == "message" else None)

    ok = True
    for message, channel, expected in _CLASSIFIER_CASES:
        req = ClassifierInput(
            request_id=f"verify-{expected}", user_id="verify", message_text=message, channel=channel
        )
        start = time.perf_counter()
        failures: list[str] = []
        got = "-"
        conf = "-"
        try:
            res = await invoker.invoke_classifier(req)
            got, conf = res.category, f"{res.confidence:.2f}"
            if not isinstance(res, Classification):
                failures.append("not a Classification")
            if res.category != expected:
                failures.append(f"category {res.category} != {expected}")
            if not 0.5 <= res.confidence <= 1.0:
                failures.append(f"confidence {res.confidence:.2f} outside [0.5, 1.0]")
            if not res.rationale.strip():
                failures.append("blank rationale")
        except Exception as exc:  # noqa: BLE001 — smoke test surfaces any error
            detail = f"{type(exc).__name__}: {exc}"
            resp = getattr(exc, "response", None)
            if resp is not None:
                detail += f"\nbody: {resp.text[:500]}"
            failures.append(detail)

        ms = (time.perf_counter() - start) * 1000
        ok &= not failures
        verdict = "[green]PASS[/green]" if not failures else "[red]FAIL: " + "; ".join(failures) + "[/red]"
        table.add_row(message, str(expected), str(got), conf, f"{ms:.0f}", verdict)

    console.print(table)
    return ok


async def _verify_resolver() -> bool:
    settings = get_settings()
    invoker = build_invoker(settings, "remote")
    table = Table(title="resolver — remote smoke test", show_lines=True)
    for col in ("message", "cat", "expected", "got", "cites", "ms", "result"):
        table.add_column(col, overflow="fold", max_width=42 if col == "message" else None)

    ok = True
    for message, category, expected_status, expected_reason in _RESOLVER_CASES:
        req = ResolverInput(request_id=f"verify-{category}", category=category, message_text=message)
        start = time.perf_counter()
        failures: list[str] = []
        got = "-"
        n_cites = "-"
        try:
            res = await invoker.invoke_resolver(req)
            got = f"{res.status}/{res.escalation_reason or '-'}"
            n_cites = str(len(res.citations))
            if not isinstance(res, ResolverOutput):
                failures.append("not a ResolverOutput")
            if res.status != expected_status:
                failures.append(f"status {res.status} != {expected_status}")
            if expected_reason and res.escalation_reason != expected_reason:
                failures.append(f"reason {res.escalation_reason} != {expected_reason}")
            if res.status == "answered" and (not res.answer or not res.citations):
                failures.append("answered without answer/citations")
        except Exception as exc:  # noqa: BLE001 — smoke test surfaces any error
            detail = f"{type(exc).__name__}: {exc}"
            resp = getattr(exc, "response", None)
            if resp is not None:
                detail += f"\nbody: {resp.text[:500]}"
            failures.append(detail)

        ms = (time.perf_counter() - start) * 1000
        ok &= not failures
        verdict = "[green]PASS[/green]" if not failures else "[red]FAIL: " + "; ".join(failures) + "[/red]"
        expected = f"{expected_status}/{expected_reason or '-'}"
        table.add_row(message, str(category), expected, got, n_cites, f"{ms:.0f}", verdict)

    console.print(table)
    return ok


_VERIFIERS = {"classifier": _verify_classifier, "resolver": _verify_resolver}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("agent", choices=sorted(_VERIFIERS))
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    configure_logging("DEBUG" if args.debug else "INFO")
    s = get_settings()
    url = s.resolver_responses_url() if args.agent == "resolver" else s.classifier_responses_url()
    console.print(f"[dim]verifying[/dim] {args.agent}  [dim]url=[/dim]{url}")

    ok = await _VERIFIERS[args.agent]()
    console.print("[green bold]all passed[/green bold]" if ok else "[red bold]checks failed[/red bold]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
