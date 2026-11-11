from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx

from app.config import corpus_dir

logger = logging.getLogger(__name__)


def fetch_sources() -> dict[str, dict[str, str | int | None]]:
    manifest = json.loads((Path(corpus_dir()) / "sources.json").read_text(encoding="utf-8"))
    destination = Path(os.getenv("CORPUS_PDF_DIR", "data/source_pdfs"))
    destination.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, str | int | None]] = {}
    for source in manifest["sources"]:
        path = destination / f"{source['document_id']}.pdf"
        if not path.exists() and os.getenv("CORPUS_FETCH_REMOTE", "true").lower() not in ("1", "true", "yes"):
            result[source["document_id"]] = {"status": "not_downloaded", "path": None}
            continue
        try:
            if not path.exists() or os.getenv("CORPUS_REFRESH", "").lower() in ("1", "true", "yes"):
                with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True) as client:
                    response = client.get(source["url"])
                    response.raise_for_status()
                    if not response.content.startswith(b"%PDF"):
                        raise ValueError("source response is not a PDF")
                    path.write_bytes(response.content)
            data = path.read_bytes()
            result[source["document_id"]] = {
                "status": "ready",
                "path": str(path),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        except Exception as exc:
            logger.warning(
                "Could not fetch policy source; offline excerpts remain available",
                extra={"step": source["document_id"]},
            )
            result[source["document_id"]] = {"status": f"unavailable: {type(exc).__name__}", "path": None}
    return result
