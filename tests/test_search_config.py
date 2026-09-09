"""Search-related ``Settings`` behaviour. Hermetic."""

from __future__ import annotations

import pytest

from helpdesk.config import Settings
from helpdesk.contracts import Category


def test_index_for_maps_categories() -> None:
    s = Settings()
    assert s.index_for(Category.support) == "support-index"
    assert s.index_for(Category.hr) == "hr-index"
    assert s.index_for("support") == "support-index"


def test_index_for_billing_raises() -> None:
    with pytest.raises(ValueError, match="billing"):
        Settings().index_for(Category.billing)


def test_embedding_defaults() -> None:
    s = Settings()
    assert s.embedding_model == "text-embedding-3-small"
    assert s.embedding_dimensions == 1536
    assert s.search_query_type == "vector_semantic_hybrid"
