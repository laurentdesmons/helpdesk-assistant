"""Chunking tests — run against the real in-repo ``docs/`` corpus. Hermetic."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from helpdesk.search.chunking import CHUNK_ID_RE, iter_chunks, parse_doc

DOCS = Path("docs")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def test_corpus_chunks_to_expected_counts() -> None:
    chunks = iter_chunks(DOCS)
    assert len(chunks) == 24  # 6 docs x 4 H2 sections
    by_category = Counter(c.category for c in chunks)
    assert by_category == {"support": 12, "hr": 12}


def test_every_doc_contributes_four_chunks() -> None:
    per_doc = Counter(c.doc_id for c in iter_chunks(DOCS))
    assert set(per_doc.values()) == {4}
    assert len(per_doc) == 6


def test_chunk_ids_unique_and_key_safe() -> None:
    chunks = iter_chunks(DOCS)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))
    assert all(CHUNK_ID_RE.match(cid) for cid in ids)


def test_sections_match_source_headings() -> None:
    for path in DOCS.glob("*.md"):
        headings = set(_H2_RE.findall(path.read_text("utf-8")))
        for chunk in parse_doc(path):
            assert chunk.section in headings


def test_content_carries_doc_context_and_is_non_empty() -> None:
    for chunk in iter_chunks(DOCS):
        assert chunk.content.startswith(f"{chunk.title} > {chunk.section}")
        assert chunk.content.split("\n\n", 1)[1].strip()


def test_frontmatter_is_parsed() -> None:
    chunk = parse_doc(DOCS / "parental-leave-policy.md")[0]
    assert chunk.category == "hr"
    assert chunk.doc_type == "policy"
    assert chunk.last_updated == "2026-01-10"
    assert chunk.title == "Parental Leave Policy"


def test_iter_chunks_is_deterministic() -> None:
    assert [c.chunk_id for c in iter_chunks(DOCS)] == [c.chunk_id for c in iter_chunks(DOCS)]


def test_missing_frontmatter_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("# No Frontmatter\n\n## A\nbody\n", "utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        parse_doc(bad)
