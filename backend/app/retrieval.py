"""BM25 retrieval over the document corpus, authority-aware and account-scoped.

Design decisions:
- BM25 (implemented locally, no external service) rather than embeddings: the
  corpus is six short documents, keyword search is transparent and auditable,
  and the agent can reformulate queries. No network dependency at query time.
- Deprecated documents are EXCLUDED by default. Customers can never see them;
  staff can opt in with include_deprecated=true and results are loudly labeled.
- Results carry the authority metadata (tier, status, effective dates, account
  binding) so the model can apply source precedence and cite what it used.
- A small authority boost nudges agreements/current policy above product docs
  when scores tie; it never resurrects deprecated content.
"""

from __future__ import annotations

import math
from collections import Counter

from . import corpus
from .corpus import Chunk

K1 = 1.5
B = 0.75

# Mild score multiplier by authority tier (1 = highest authority).
_TIER_BOOST = {1: 1.25, 2: 1.15, 3: 1.0, 9: 1.0}


def _bm25_scores(query_tokens: list[str], chunks: list[Chunk]) -> list[float]:
    n = len(chunks)
    if n == 0:
        return []
    avgdl = sum(len(c.tokens) for c in chunks) / n
    df: Counter[str] = Counter()
    for c in chunks:
        for term in set(c.tokens):
            df[term] += 1
    scores = []
    for c in chunks:
        tf = Counter(c.tokens)
        score = 0.0
        for term in query_tokens:
            if df[term] == 0:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            freq = tf[term]
            denom = freq + K1 * (1 - B + B * len(c.tokens) / avgdl)
            score += idf * (freq * (K1 + 1)) / denom if denom else 0.0
        scores.append(score)
    return scores


def search(
    query: str,
    account_scope: str | None,
    include_deprecated: bool = False,
    top_k: int = 5,
) -> dict:
    """Search the corpus. Returns scored chunks with authority metadata."""
    query_tokens = corpus.tokenize_query(query)

    visible: list[Chunk] = []
    for chunk in corpus.all_chunks():
        meta = corpus.get_doc_meta(chunk.doc_id)
        if not corpus.doc_visible(meta, account_scope):
            continue  # other accounts' agreements never enter the candidate set
        if meta["status"] == "deprecated" and not include_deprecated:
            continue
        visible.append(chunk)

    scores = _bm25_scores(query_tokens, visible)
    ranked = sorted(zip(visible, scores), key=lambda pair: -pair[1] * _boost(pair[0]))
    results = []
    for chunk, score in ranked[:top_k]:
        if score <= 0:
            continue
        meta = corpus.get_doc_meta(chunk.doc_id)
        entry = {
            "doc_id": chunk.doc_id,
            "title": meta["title"],
            "section": chunk.section,
            "status": meta["status"],
            "authority_tier": meta["authority_tier"],
            "authority": meta["authority_label"],
            "effective_from": meta.get("effective_from"),
            "text": chunk.text,
            "score": round(score, 3),
        }
        if meta.get("account_id"):
            entry["applies_to_account"] = meta["account_id"]
        if meta["status"] == "deprecated":
            entry["warning"] = (
                "DEPRECATED DOCUMENT - historical reference only. Never base a "
                f"current answer on this; use {meta.get('superseded_by')} instead."
            )
        results.append(entry)

    note = (
        "Source precedence (Support Policy v3 §1): signed customer agreement > "
        "current support policy/SOP > current product documentation. Historical "
        "tickets and internal notes are context only."
    )
    hidden_deprecated = sum(
        1
        for c in corpus.all_chunks()
        if corpus.get_doc_meta(c.doc_id)["status"] == "deprecated"
    )
    payload: dict = {"query": query, "results": results, "precedence_note": note}
    if not include_deprecated and hidden_deprecated:
        payload["excluded"] = (
            "Deprecated documents (e.g. Support Policy v2) were excluded from this search."
        )
    return payload


def _boost(chunk: Chunk) -> float:
    tier = corpus.get_doc_meta(chunk.doc_id)["authority_tier"]
    return _TIER_BOOST.get(tier, 1.0)
