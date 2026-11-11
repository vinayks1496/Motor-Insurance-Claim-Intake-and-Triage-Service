from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import redis
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from sklearn.feature_extraction.text import HashingVectorizer

from app.config import index_dir, redis_url
from app.metrics import retrieval_cache_hits, tool_calls

logger = logging.getLogger(__name__)


class LocalHashEmbeddings(Embeddings):
    """Deterministic local word-unigram/bigram hashing; no remote model or key is required."""

    def __init__(self) -> None:
        self.vectorizer = HashingVectorizer(
            n_features=1024,
            alternate_sign=False,
            norm="l2",
            stop_words="english",
            ngram_range=(1, 2),
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        matrix = self.vectorizer.transform(texts)
        return matrix.astype("float32").toarray().tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class CorpusIndex:
    def __init__(self) -> None:
        self.directory = Path(index_dir())
        self.embeddings = LocalHashEmbeddings()
        self.index = FAISS.load_local(
            str(self.directory / "faiss"),
            self.embeddings,
            allow_dangerous_deserialization=True,
        )
        passages_path = self.directory / "passages.json"
        rows = json.loads(passages_path.read_text(encoding="utf-8"))
        self.passages = {row["passage_id"]: row for row in rows}
        self.redis = redis.Redis.from_url(redis_url(), decode_responses=True)

    def search(self, query: str, k: int = 6) -> list[dict[str, Any]]:
        tool_calls.labels(tool="search_policy").inc()
        normalized = " ".join(query.lower().split())
        cache_key = "retrieval:v2:" + hashlib.sha256(normalized.encode()).hexdigest()
        cached = self.redis.get(cache_key)
        if cached:
            retrieval_cache_hits.inc()
            return json.loads(cached)
        rows = []
        for document, distance in self.index.similarity_search_with_score(query, k=k):
            relevance = max(0.0, min(1.0, 1.0 - float(distance) / 2.0))
            rows.append(
                {
                    "passage_id": document.metadata["passage_id"],
                    "document_id": document.metadata.get("document_id"),
                    "document_title": document.metadata.get("document_title"),
                    "source_url": document.metadata.get("source_url"),
                    "page": document.metadata.get("page"),
                    "section": document.metadata.get("section"),
                    "document_type": document.metadata.get("document_type"),
                    "text": document.page_content,
                    "relevance": round(relevance, 4),
                }
            )
        self.redis.setex(cache_key, 300, json.dumps(rows, ensure_ascii=False))
        return rows

    def get_clause(self, clause_id: str) -> dict[str, Any] | None:
        tool_calls.labels(tool="get_clause_by_id").inc()
        return self.passages.get(clause_id)

    @staticmethod
    def get_fnol_field(fields: dict[str, Any], field_name: str) -> Any:
        tool_calls.labels(tool="get_fnol_fields").inc()
        return fields.get(field_name)

    def status(self) -> dict[str, Any]:
        manifest_path = self.directory / "manifest.json"
        if not manifest_path.exists():
            return {"ready": False, "chunks": 0}
        return json.loads(manifest_path.read_text(encoding="utf-8"))


def passage_document(
    passage_id: str,
    text: str,
    metadata: dict[str, Any],
) -> Document:
    return Document(
        page_content=text.strip(),
        metadata={"passage_id": passage_id, **metadata},
    )
