"""Build the Azure AI Search knowledge-base indexes from ``docs/``.

    uv run python scripts/build_kb.py --recreate
    uv run python scripts/build_kb.py --dry-run            # chunk only, no network
    uv run python scripts/build_kb.py --category hr --recreate

Thin driver: chunking / embedding / upload all live in
``helpdesk.search.pipeline``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from helpdesk.config import get_settings
from helpdesk.logging import configure_logging
from helpdesk.search.pipeline import BuildReport, build_knowledge_base

console = Console()


def _render(report: BuildReport) -> None:
    table = Table(title="knowledge base build", show_lines=True)
    for col in ("index", "category", "chunks", "docs", "per-doc chunks", "uploaded"):
        table.add_column(col, overflow="fold")
    for idx in report.indexes:
        per_doc = ", ".join(f"{k}={v}" for k, v in sorted(idx.per_doc.items())) or "-"
        table.add_row(
            idx.index,
            idx.category,
            str(idx.n_chunks),
            str(idx.n_docs),
            per_doc,
            "[green]yes[/green]" if idx.uploaded else "[dim]no[/dim]",
        )
    console.print(table)
    mode = "dry-run" if report.dry_run else ("recreate" if report.recreate else "merge")
    console.print(
        f"[dim]mode=[/dim]{mode}  [dim]dims=[/dim]{report.dimensions}  "
        f"[dim]elapsed=[/dim]{report.elapsed_s:.1f}s"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-dir", type=Path, default=Path("docs"))
    ap.add_argument("--category", choices=("support", "hr", "all"), default="all")
    ap.add_argument("--recreate", action="store_true", help="drop and rebuild the index schema")
    ap.add_argument("--dry-run", action="store_true", help="chunk and report only; no network")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    configure_logging("DEBUG" if args.debug else "INFO")
    settings = get_settings()
    categories = None if args.category == "all" else [args.category]

    report = await build_knowledge_base(
        settings,
        args.docs_dir,
        recreate=args.recreate,
        categories=categories,
        dry_run=args.dry_run,
    )
    _render(report)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
