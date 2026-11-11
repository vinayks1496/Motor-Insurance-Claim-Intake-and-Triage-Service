from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from pypdf import PdfReader

from app.config import corpus_dir, index_dir
from app.retrieval import LocalHashEmbeddings
from app.scripts.fetch_corpus import fetch_sources

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHUNK_WORDS = 190
OVERLAP_WORDS = 32
STRATEGY = "word-and-bigram-hashing-1024; page-aware; 190-word windows; 32-word overlap; v2"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _split(text: str) -> list[str]:
    words = re.sub(r"\s+", " ", text).strip().split()
    if not words:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(len(words), start + CHUNK_WORDS)
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start = end - OVERLAP_WORDS
    return chunks


def _markdown_passages(path: Path) -> list[Document]:
    raw = path.read_text(encoding="utf-8")
    frontmatter = {}
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end >= 0:
            for line in raw[3:end].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    frontmatter[key.strip()] = value.strip()
            raw = raw[end + 4 :]
    document_id = frontmatter.get("document_id", path.stem)
    title = frontmatter.get("document_title", path.stem)
    url = frontmatter.get("source_url", "")
    kind = frontmatter.get("document_type", "policy")
    sections = re.split(r"(?m)^##\s+", raw)
    docs = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        lines = section.splitlines()
        section_name = lines[0].strip("# ").strip()
        content = "\n".join(lines[1:]).strip() if len(lines) > 1 else section
        if not content:
            continue
        safe_id = re.sub(r"[^A-Za-z0-9-]+", "-", section_name).strip("-").upper()
        for number, chunk in enumerate(_split(content), start=1):
            passage_id = safe_id if number == 1 else f"{safe_id}-PART-{number}"
            docs.append(
                Document(
                    page_content=f"{section_name}\n{chunk}",
                    metadata={
                        "passage_id": passage_id,
                        "document_id": document_id,
                        "document_title": title,
                        "document_type": kind,
                        "source_url": url,
                        "section": section_name,
                        "page": None,
                        "corpus_source": "offline_excerpt",
                    },
                )
            )
    return docs


def _pdf_pages(source: dict[str, Any], path: Path) -> list[Document]:
    reader = PdfReader(str(path))
    requested = source.get("pages", "all")
    indexes = range(len(reader.pages)) if requested == "all" else [int(i) for i in requested]
    docs = []
    for page_index in indexes:
        if page_index >= len(reader.pages):
            continue
        text = reader.pages[page_index].extract_text() or ""
        for number, chunk in enumerate(_split(text), start=1):
            passage_id = f"{source['document_id']}-P{page_index + 1}-C{number}"
            docs.append(
                Document(
                    page_content=chunk,
                    metadata={
                        "passage_id": passage_id,
                        "document_id": source["document_id"],
                        "document_title": source["title"],
                        "document_type": source["type"],
                        "source_url": source["url"],
                        "section": None,
                        "page": page_index + 1,
                        "corpus_source": "official_pdf",
                    },
                )
            )
    return docs


def build_index() -> dict[str, Any]:
    started = time.perf_counter()
    corpus_path = Path(corpus_dir())
    output = Path(index_dir())
    output.mkdir(parents=True, exist_ok=True)
    download_status = fetch_sources()
    source_manifest = json.loads((corpus_path / "sources.json").read_text(encoding="utf-8"))
    documents: list[Document] = []
    versions: dict[str, str] = {}

    downloaded = {
        key: Path(str(value["path"]))
        for key, value in download_status.items()
        if value.get("status") == "ready" and value.get("path")
    }
    for source in source_manifest["sources"]:
        source_id = source["document_id"]
        if source_id in downloaded:
            file_path = downloaded[source_id]
            file_bytes = file_path.read_bytes()
            versions[source_id] = _hash(file_bytes)
            documents.extend(_pdf_pages(source, file_path))
        else:
            versions[source_id] = "offline-fallback"

    for path in sorted(corpus_path.glob("*.md")):
        if path.name == "README.md":
            continue
        for doc in _markdown_passages(path):
            source_id = doc.metadata["document_id"]
            if source_id not in downloaded:
                documents.append(doc)
        versions[path.name] = _hash(path.read_bytes())

    if not documents:
        raise RuntimeError("No policy documents were available to index")
    fingerprint = hashlib.sha256(
        json.dumps({"sources": versions, "strategy": STRATEGY}, sort_keys=True).encode()
    ).hexdigest()
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and os.getenv("FORCE_REEMBED", "").lower() not in ("1", "true", "yes"):
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("fingerprint") == fingerprint and (output / "faiss" / "index.faiss").exists():
            logger.info("Corpus index is current; skipping re-embedding")
            return old

    index = FAISS.from_documents(documents, LocalHashEmbeddings())
    index.save_local(str(output / "faiss"))
    passage_rows = []
    for document in documents:
        passage_rows.append(
            {
                "passage_id": document.metadata["passage_id"],
                "document_id": document.metadata.get("document_id"),
                "document_title": document.metadata.get("document_title"),
                "document_type": document.metadata.get("document_type"),
                "source_url": document.metadata.get("source_url"),
                "section": document.metadata.get("section"),
                "page": document.metadata.get("page"),
                "text": document.page_content,
                "corpus_source": document.metadata.get("corpus_source"),
            }
        )
    (output / "passages.json").write_text(json.dumps(passage_rows, ensure_ascii=False), encoding="utf-8")
    duration = max(0.001, time.perf_counter() - started)
    result = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fingerprint,
        "source_versions": versions,
        "source_fetch_status": download_status,
        "chunking_strategy": STRATEGY,
        "chunks": len(documents),
        "build_seconds": round(duration, 3),
        "throughput_chunks_per_second": round(len(documents) / duration, 2),
        "embedding_strategy": "local scikit-learn word and bigram feature hashing",
        "reembedding": os.getenv("FORCE_REEMBED", "").lower() in ("1", "true", "yes"),
    }
    manifest_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Built FAISS index", extra={"step": result})
    return result


if __name__ == "__main__":
    build_index()
