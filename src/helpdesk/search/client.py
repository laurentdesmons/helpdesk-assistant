"""The knowledge-base retrieval seam.

``KnowledgeBaseSearch.search(category, query)`` is the single entry point the
Phase 3 resolver wraps in its ``@ai_function`` tool. Pure constructor: no client
and no credential until the first ``search`` call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from helpdesk.config import Settings
from helpdesk.contracts import Category, Citation
from helpdesk.search.embeddings import Embedder

if TYPE_CHECKING:
    from azure.identity.aio import DefaultAzureCredential
    from azure.search.documents.aio import SearchClient

logger = logging.getLogger("helpdesk.search.client")

_RETRIEVAL_FIELDS = "chunk_id,doc_id,title,section,content,source_path,doc_type,last_updated"


class SearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    doc_id: str
    title: str
    section: str
    content: str
    source_path: str
    score: float
    reranker_score: float | None = None

    def to_citation(self) -> Citation:
        return Citation(
            doc_id=self.doc_id,
            title=f"{self.title} > {self.section}",
            snippet=self.content[:280],
            score=self.reranker_score if self.reranker_score is not None else self.score,
        )


class KnowledgeBaseSearch:
    def __init__(self, settings: Settings) -> None:
        settings.require("search_endpoint")
        assert settings.search_endpoint is not None  # narrowed by require()
        self._settings = settings
        self._endpoint = settings.search_endpoint
        self._embedder = Embedder(settings)
        self._credential: DefaultAzureCredential | None = None
        self._clients: dict[str, SearchClient] = {}

    def _client(self, index: str) -> SearchClient:
        if index not in self._clients:
            from azure.identity.aio import DefaultAzureCredential
            from azure.search.documents.aio import SearchClient

            if self._credential is None:
                self._credential = DefaultAzureCredential()
            self._clients[index] = SearchClient(
                endpoint=self._endpoint, index_name=index, credential=self._credential
            )
        return self._clients[index]

    async def search(
        self, category: Category | str, query: str, top_k: int | None = None
    ) -> list[SearchResult]:
        index = self._settings.index_for(category)
        k = top_k or self._settings.search_top_k
        mode = self._settings.search_query_type
        client = self._client(index)

        kwargs: dict[str, Any] = {"top": k, "select": _RETRIEVAL_FIELDS, "search_text": query}
        if mode != "keyword":
            from azure.search.documents.models import VectorizedQuery

            vector = await self._embedder.embed_query(query)
            kwargs["vector_queries"] = [
                VectorizedQuery(vector=vector, k_nearest_neighbors=k, fields="content_vector")
            ]
            if mode == "vector_semantic_hybrid":
                kwargs["query_type"] = "semantic"
                kwargs["semantic_configuration_name"] = self._settings.search_semantic_config

        results: list[SearchResult] = []
        async for doc in await client.search(**kwargs):
            results.append(
                SearchResult(
                    chunk_id=doc["chunk_id"],
                    doc_id=doc["doc_id"],
                    title=doc["title"],
                    section=doc["section"],
                    content=doc["content"],
                    source_path=doc.get("source_path", ""),
                    score=doc["@search.score"],
                    reranker_score=doc.get("@search.reranker_score"),
                )
            )
        logger.info("kb search [%s/%s] %r -> %d hits", category, mode, query, len(results))
        return results

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        if self._credential is not None:
            await self._credential.close()
            self._credential = None
        await self._embedder.aclose()


def build_kb_search(settings: Settings) -> KnowledgeBaseSearch:
    """Pure constructor for the KB search client."""
    return KnowledgeBaseSearch(settings)
