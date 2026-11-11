from __future__ import annotations

import hashlib
from typing import Any


def score_live_run(result: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, float]:
    verification = result.get("verification", {})
    citations = result.get("citations", [])
    citation_ids = {row.get("passage_id") for row in citations if isinstance(row, dict)}
    all_ids = {row.get("passage_id") for row in evidence}
    groundedness = 1.0 if verification.get("verified") and citation_ids and citation_ids <= all_ids else 0.0
    context_relevance = len(citation_ids & all_ids) / max(1, len(all_ids))
    faithfulness = float(verification.get("lexical_support", 0.0)) if groundedness else 0.0
    hallucination_rate = 1.0 - groundedness
    return {
        "groundedness": round(max(0.0, min(1.0, groundedness)), 4),
        "context_relevance": round(max(0.0, min(1.0, context_relevance)), 4),
        "faithfulness": round(max(0.0, min(1.0, faithfulness)), 4),
        "hallucination_rate": round(max(0.0, min(1.0, hallucination_rate)), 4),
    }


def sample_this_run(run_id: str, denominator: int = 10) -> bool:
    digest = hashlib.sha256(run_id.encode()).digest()
    return int.from_bytes(digest[:2], "big") % denominator == 0
