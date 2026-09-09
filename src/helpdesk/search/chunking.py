"""Markdown -> chunks. Pure: reads files, no network, no model calls.

Each ``docs/*.md`` file is one KB document with flat ``key: value`` YAML-ish
frontmatter and a body of ``## `` sections. We chunk at H2: one :class:`Chunk`
per section, the pre-first-H2 preamble (just the ``# H1``) dropped. Sections here
are short and self-contained, so this is the natural retrieval unit.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.contracts import Category

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_KEY_SAFE_RE = re.compile(r"[^a-z0-9]+")

# Azure AI Search document keys: letters, digits, dash, underscore, equals only.
CHUNK_ID_RE = re.compile(r"^[A-Za-z0-9_\-=]+$")


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    chunk_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    section: str = Field(min_length=1)
    category: Category
    doc_type: str = Field(min_length=1)
    last_updated: str
    source_path: str
    content: str = Field(min_length=1)


def _slug(text: str) -> str:
    return _KEY_SAFE_RE.sub("-", text.lower()).strip("-")


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Return ``(frontmatter dict, body)``. Frontmatter is flat ``key: value``."""
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError("missing '---' frontmatter block")
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm, raw[m.end() :]


def parse_doc(path: Path) -> list[Chunk]:
    """Chunk one markdown file at its ``## `` headings."""
    raw = path.read_text("utf-8")
    fm, body = _parse_frontmatter(raw)

    for required in ("category", "title", "doc_type"):
        if not fm.get(required):
            raise ValueError(f"{path.name}: frontmatter missing '{required}'")
    category = Category(fm["category"])  # raises on an unknown category
    title = fm["title"]
    doc_id = path.stem

    matches = list(_H2_RE.finditer(body))
    if not matches:
        raise ValueError(f"{path.name}: no '## ' sections to chunk")

    chunks: list[Chunk] = []
    for i, m in enumerate(matches):
        section = m.group(1).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body = body[m.end() : end].strip()
        if not section_body:
            continue
        chunk_id = f"{_slug(doc_id)}__{_slug(section)}"
        if not CHUNK_ID_RE.match(chunk_id):  # pragma: no cover - defensive
            raise ValueError(f"{path.name}: derived chunk_id {chunk_id!r} is not key-safe")
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                title=title,
                section=section,
                category=category,
                doc_type=fm["doc_type"],
                last_updated=fm.get("last_updated", ""),
                source_path=str(path),
                # Prefix with "title > section" so a bare chunk still carries doc
                # context to the semantic ranker and, later, the resolver.
                content=f"{title} > {section}\n\n{section_body}",
            )
        )
    return chunks


def iter_chunks(docs_dir: Path) -> list[Chunk]:
    """All chunks under ``docs_dir``, deterministically ordered."""
    all_chunks: list[Chunk] = []
    for path in sorted(docs_dir.glob("*.md")):
        all_chunks.extend(parse_doc(path))
    return all_chunks
