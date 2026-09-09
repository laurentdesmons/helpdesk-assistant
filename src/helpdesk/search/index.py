"""Index schema + lifecycle (Azure AI Search control plane).

One schema, two indexes (``support-index`` / ``hr-index``). Hybrid retrieval:
keyword (BM25) + vector (HNSW, cosine) + optional semantic ranker. Vectors are
pushed by ``pipeline.build_knowledge_base`` — no indexer, no skillset.
"""

from __future__ import annotations

import logging

from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)

from helpdesk.config import Settings

logger = logging.getLogger("helpdesk.search.index")

_HNSW_CONFIG = "helpdesk-hnsw"
_VECTOR_PROFILE = "helpdesk-vector-profile"


def build_index(name: str, settings: Settings) -> SearchIndex:
    fields = [
        SimpleField(name="chunk_id", type=SearchFieldDataType.String, key=True, filterable=True),
        SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="section", type=SearchFieldDataType.String),
        SimpleField(name="category", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="doc_type", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="last_updated", type=SearchFieldDataType.String),
        SimpleField(name="source_path", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=settings.embedding_dimensions,
            vector_search_profile_name=_VECTOR_PROFILE,
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name=_HNSW_CONFIG)],
        profiles=[
            VectorSearchProfile(
                name=_VECTOR_PROFILE, algorithm_configuration_name=_HNSW_CONFIG
            )
        ],
    )
    semantic_search = SemanticSearch(
        configurations=[
            SemanticConfiguration(
                name=settings.search_semantic_config,
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    keywords_fields=[SemanticField(field_name="section")],
                    content_fields=[SemanticField(field_name="content")],
                ),
            )
        ]
    )
    return SearchIndex(
        name=name, fields=fields, vector_search=vector_search, semantic_search=semantic_search
    )


def ensure_index(
    client: SearchIndexClient, name: str, settings: Settings, *, recreate: bool
) -> None:
    if recreate:
        try:
            client.delete_index(name)
            logger.info("deleted existing index %s", name)
        except Exception:  # noqa: BLE001 - index may not exist yet
            logger.debug("no existing index %s to delete", name)
    client.create_or_update_index(build_index(name, settings))
    logger.info("index %s created/updated", name)
