"""Document corpus: loads extracted policy/agreement text with authority metadata.

Each document from the pack is registered in data/structured/doc_registry.json
with an authority tier (Support Policy v3 §1 source precedence):

  tier 1  signed customer agreement (account-bound)
  tier 2  current support policy / SOP
  tier 3  current product documentation
  tier 9  deprecated (context only, quarantined from normal retrieval)

Account-bound agreements are only visible to their own account or to staff —
enforced here, in the data layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from . import config, datastore
from .datastore import AccessDenied


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    section: str
    text: str
    tokens: list[str] = field(default_factory=list)


_SECTION_RE = re.compile(r"^\s*(\d+\.\s+.+?)\s*$")


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9\-]*", text.lower())


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split extracted PDF text into (section_title, body) pairs."""
    lines = [ln for ln in text.splitlines() if not ln.startswith("[page ")]
    sections: list[tuple[str, list[str]]] = [("Header", [])]
    for line in lines:
        m = _SECTION_RE.match(line)
        if m and len(m.group(1)) < 90:
            sections.append((m.group(1), []))
        else:
            sections[-1][1].append(line)
    return [(title, "\n".join(body).strip()) for title, body in sections if "\n".join(body).strip()]


@lru_cache(maxsize=1)
def _index() -> dict:
    docs: dict[str, dict] = {}
    chunks: list[Chunk] = []
    for meta in datastore.doc_registry():
        path = config.CORPUS_DIR / meta["file"]
        text = path.read_text(encoding="utf-8")
        docs[meta["doc_id"]] = {**meta, "text": text}
        for i, (title, body) in enumerate(_split_sections(text)):
            chunk_text = f"{title}\n{body}" if title != "Header" else body
            chunks.append(
                Chunk(
                    chunk_id=f"{meta['doc_id']}#{i}",
                    doc_id=meta["doc_id"],
                    section=title,
                    text=chunk_text,
                    tokens=_tokenize(f"{meta['title']} {chunk_text}"),
                )
            )
    return {"docs": docs, "chunks": chunks}


def all_chunks() -> list[Chunk]:
    return _index()["chunks"]


def get_doc_meta(doc_id: str) -> dict:
    doc = _index()["docs"].get(doc_id)
    if doc is None:
        raise KeyError(f"No document with id {doc_id!r}")
    return {k: v for k, v in doc.items() if k != "text"}


def doc_visible(meta: dict, account_scope: str | None) -> bool:
    """Account-bound docs are visible only to their account or to staff."""
    bound = meta.get("account_id")
    return bound is None or account_scope is None or bound == account_scope


def read_document(doc_id: str, account_scope: str | None, allow_deprecated: bool = False) -> dict:
    doc = _index()["docs"].get(doc_id)
    # Only ever hint at docs the caller may actually see, so a bad id can't
    # disclose the existence/names of other accounts' agreements (or deprecated docs).
    visible_ids = sorted(
        d
        for d, m in _index()["docs"].items()
        if doc_visible(m, account_scope) and (allow_deprecated or m["status"] != "deprecated")
    )
    if doc is None or not doc_visible(doc, account_scope):
        # Uniform response whether the id is unknown or simply out of scope —
        # existence of another account's document is not observable.
        raise KeyError(
            f"No document with id {doc_id!r}. Valid ids: " + ", ".join(visible_ids)
        )
    if doc["status"] == "deprecated" and not allow_deprecated:
        raise AccessDenied(
            f"Document {doc_id} is DEPRECATED and quarantined from normal use. "
            f"It is superseded by {doc.get('superseded_by')}. Staff may pass "
            "include_deprecated=true to read it for historical reference only."
        )
    meta = {k: v for k, v in doc.items() if k != "text"}
    return {**meta, "text": doc["text"]}


def tokenize_query(query: str) -> list[str]:
    return _tokenize(query)
