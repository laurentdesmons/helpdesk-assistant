"""Query a knowledge-base index directly — the manual Phase 2 verification tool.

    uv run python scripts/search_kb.py --category support --query "my vpn keeps dropping"
    uv run python scripts/search_kb.py --category hr --query "carry over vacation" --query-type keyword

Thin driver: retrieval lives in ``helpdesk.search.client.KnowledgeBaseSearch``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

from helpdesk.config import get_settings
from helpdesk.logging import configure_logging
from helpdesk.search.client import build_kb_search

console = Console()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", required=True, choices=("support", "hr", "billing"))
    ap.add_argument("--query", required=True)
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument(
        "--query-type", choices=("vector_semantic_hybrid", "vector_hybrid", "keyword"), default=None
    )
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    configure_logging("DEBUG" if args.debug else "WARNING")
    settings = get_settings()
    if args.query_type:
        settings = settings.model_copy(update={"search_query_type": args.query_type})

    kb = build_kb_search(settings)
    try:
        results = await kb.search(args.category, args.query, args.top_k)
    except ValueError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return 0
    finally:
        await kb.aclose()

    table = Table(title=f"{args.category}  ·  {args.query!r}", show_lines=True)
    for col in ("#", "score", "reranker", "doc", "section", "snippet"):
        table.add_column(col, overflow="fold", max_width=60 if col == "snippet" else None)
    for i, r in enumerate(results, 1):
        table.add_row(
            str(i),
            f"{r.score:.3f}",
            f"{r.reranker_score:.3f}" if r.reranker_score is not None else "-",
            r.doc_id,
            r.section,
            r.content.split("\n\n", 1)[-1][:200],
        )
    console.print(table)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
