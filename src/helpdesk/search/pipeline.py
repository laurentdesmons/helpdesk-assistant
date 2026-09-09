"""Build the KB: chunk ``docs/`` -> embed -> push to Azure AI Search.

Push model: this process computes the vectors and uploads whole documents. No
indexer, no skillset. Small corpus, full local control, rich logs.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from helpdesk.config import Settings
from helpdesk.search.chunking import Chunk, iter_chunks
from helpdesk.search.embeddings import Embedder

logger = logging.getLogger("helpdesk.search.pipeline")

# Categories that have a KB index (billing escalates, no retrieval).
_INDEXED_CATEGORIES = ("support", "hr")


class IndexReport(BaseModel):
    index: str
    category: str
    n_chunks: int
    n_docs: int
    per_doc: dict[str, int]
    uploaded: bool


class BuildReport(BaseModel):
    dry_run: bool
    recreate: bool
    dimensions: int
    elapsed_s: float
    indexes: list[IndexReport]


def _group(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    grouped: dict[str, list[Chunk]] = {c: [] for c in _INDEXED_CATEGORIES}
    for chunk in chunks:
        grouped.setdefault(str(chunk.category), []).append(chunk)
    return grouped


async def build_knowledge_base(
    settings: Settings,
    docs_dir: Path,
    *,
    recreate: bool = False,
    categories: list[str] | None = None,
    dry_run: bool = False,
) -> BuildReport:
    start = time.perf_counter()
    targets = categories or list(_INDEXED_CATEGORIES)
    grouped = _group(iter_chunks(docs_dir))

    if dry_run:
        dry_reports = [
            _report(settings.index_for(cat), cat, grouped.get(cat, []), uploaded=False)
            for cat in targets
        ]
        for r in dry_reports:
            logger.info("[dry-run] %s: %d chunks from %d docs", r.index, r.n_chunks, r.n_docs)
        return _build_report(settings, start, dry_run=True, recreate=recreate, indexes=dry_reports)

    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient

    from helpdesk.search.index import ensure_index

    settings.require("search_endpoint")
    assert settings.search_endpoint is not None  # narrowed by require()
    endpoint = settings.search_endpoint
    credential = DefaultAzureCredential()
    embedder = Embedder(settings)
    index_client = SearchIndexClient(endpoint=endpoint, credential=credential)
    reports: list[IndexReport] = []
    try:
        for category in targets:
            chunks = grouped.get(category, [])
            index_name = settings.index_for(category)
            if not chunks:
                logger.warning("no chunks for %s — skipping %s", category, index_name)
                reports.append(_report(index_name, category, chunks, uploaded=False))
                continue

            ensure_index(index_client, index_name, settings, recreate=recreate)
            vectors = await embedder.embed([c.content for c in chunks])
            documents = [
                {**chunk.model_dump(), "content_vector": vector}
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
            search_client = SearchClient(
                endpoint=endpoint, index_name=index_name, credential=credential
            )
            try:
                outcomes = search_client.merge_or_upload_documents(documents)
                failed = [o for o in outcomes if not o.succeeded]
                if failed:
                    raise RuntimeError(
                        f"{index_name}: {len(failed)}/{len(documents)} docs failed to upload "
                        f"(first: {failed[0].key} {failed[0].error_message})"
                    )
            finally:
                search_client.close()
            logger.info(
                "%s: uploaded %d chunks from %d docs", index_name, len(chunks),
                len({c.doc_id for c in chunks}),
            )
            reports.append(_report(index_name, category, chunks, uploaded=True))
    finally:
        await embedder.aclose()
        index_client.close()
        credential.close()

    return _build_report(settings, start, dry_run=False, recreate=recreate, indexes=reports)


def _report(index: str, category: str, chunks: list[Chunk], *, uploaded: bool) -> IndexReport:
    per_doc = dict(Counter(c.doc_id for c in chunks))
    return IndexReport(
        index=index,
        category=category,
        n_chunks=len(chunks),
        n_docs=len(per_doc),
        per_doc=per_doc,
        uploaded=uploaded,
    )


def _build_report(
    settings: Settings, start: float, *, dry_run: bool, recreate: bool, indexes: list[IndexReport]
) -> BuildReport:
    return BuildReport(
        dry_run=dry_run,
        recreate=recreate,
        dimensions=settings.embedding_dimensions,
        elapsed_s=time.perf_counter() - start,
        indexes=indexes,
    )
