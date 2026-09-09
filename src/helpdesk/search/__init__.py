"""Knowledge-base retrieval (Azure AI Search) — Phase 2.

``chunking`` is pure (filesystem only). ``embeddings``, ``index``, ``client`` and
``pipeline`` build Azure clients lazily — importing this package touches no
network and no credentials.
"""

from __future__ import annotations

from helpdesk.search.chunking import Chunk, iter_chunks, parse_doc
from helpdesk.search.client import KnowledgeBaseSearch, SearchResult, build_kb_search
from helpdesk.search.pipeline import BuildReport, build_knowledge_base

__all__ = [
    "BuildReport",
    "Chunk",
    "KnowledgeBaseSearch",
    "SearchResult",
    "build_kb_search",
    "build_knowledge_base",
    "iter_chunks",
    "parse_doc",
]
