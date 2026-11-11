from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.config import prompt_version
from app.guards import inspect_text, toxic_output, unauthorized_output
from app.llm import LLMClient
from app.metrics import agent_steps, llm_retries, step_latency, tool_calls
from app.retrieval import CorpusIndex

logger = logging.getLogger(__name__)

MAX_STEPS = 7
MAX_TOOL_CALLS = 8
MAX_WALL_SECONDS = 28
TOOL_TIMEOUT_SECONDS = 4
SUPPORTED_POSITIONS = {"potentially_supported", "potentially_limited", "unclear", "outside_corpus"}
SUPPORTED_ROUTES = {"fast-track", "surveyor_assignment", "investigation"}


class CoverageState(TypedDict, total=False):
    run_id: str
    correlation_id: str
    fnol: dict[str, Any]
    extracted: dict[str, Any]
    evidence: list[dict[str, Any]]
    tool_history: list[dict[str, Any]]
    steps: int
    tool_call_count: int
    started: float
    action: str
    tool_request: dict[str, Any]
    candidate: dict[str, Any]
    verification_feedback: str
    verification_attempts: int
    verified: bool
    final: dict[str, Any]
    error: str
    usage: list[dict[str, Any]]
    prompt_version: str


def _content_words(text: str) -> set[str]:
    common = {
        "the", "a", "an", "to", "of", "and", "or", "for", "with", "from", "is", "are",
        "it", "this", "that", "in", "on", "by", "as", "must", "may", "only", "also",
    }
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 2 and word not in common}


def verify_candidate(candidate: dict[str, Any], evidence: list[dict[str, Any]]) -> tuple[bool, str, float]:
    evidence_by_id = {item["passage_id"]: item for item in evidence}
    citations = candidate.get("citations") or []
    if not citations:
        return False, "No clause citations were supplied; retrieve supporting passages or escalate.", 0.0
    unresolved = [citation for citation in citations if citation not in evidence_by_id]
    if unresolved:
        return False, f"Citations do not resolve to retrieved passages: {unresolved}.", 0.0
    summary = str(candidate.get("summary", ""))
    if not summary.strip():
        return False, "The recommendation summary is empty.", 0.0
    if unauthorized_output(summary) or toxic_output(summary):
        return False, "Output filtering found an unauthorized determination or toxic language.", 0.0
    support = " ".join(evidence_by_id[citation].get("text", "") for citation in citations)
    summary_words = _content_words(summary)
    support_words = _content_words(support)
    overlap = len(summary_words & support_words) / max(1, len(summary_words))
    if overlap < 0.10 and candidate.get("position") not in ("outside_corpus", "unclear"):
        return False, "The summary has too little lexical support in its cited passages.", overlap
    return True, "Every citation resolves and the summary overlaps cited evidence.", overlap


class CoverageAgent:
    """The second agent chooses corpus tools, searches modifying clauses, and verifies its answer."""

    def __init__(self) -> None:
        self.client = LLMClient()
        self.corpus = CorpusIndex()
        graph = StateGraph(CoverageState)
        graph.add_node("decide", self._decide)
        graph.add_node("use_tool", self._use_tool)
        graph.add_node("verify", self._verify)
        graph.set_entry_point("decide")
        graph.add_conditional_edges(
            "decide",
            self._after_decision,
            {"tool": "use_tool", "verify": "verify", "finish": END},
        )
        graph.add_edge("use_tool", "decide")
        graph.add_conditional_edges(
            "verify",
            self._after_verification,
            {"search_again": "decide", "finish": END},
        )
        self.graph = graph.compile()

    def _decide(self, state: CoverageState) -> CoverageState:
        agent_steps.labels(agent="coverage").inc()
        if state.get("steps", 0) >= MAX_STEPS or time.monotonic() - state["started"] > MAX_WALL_SECONDS:
            return {
                "action": "finish",
                "error": "Coverage reasoning reached its bounded step or wall-clock limit.",
            }
        started = time.perf_counter()
        version = prompt_version()
        prompt_path = Path(os.getenv("PROMPT_DIR", "prompts")) / "coverage_v2.txt"
        template = prompt_path.read_text(encoding="utf-8")
        prompt = template.format(
            fnol=state["fnol"],
            fields=state["extracted"],
            evidence=state.get("evidence", []),
            verification_feedback=state.get("verification_feedback", ""),
            loop_state={
                "steps": state.get("steps", 0),
                "tool_calls": state.get("tool_call_count", 0),
                "available_tools": ["search_policy", "get_clause_by_id", "get_fnol_fields"],
                "max_steps": MAX_STEPS,
                "max_tool_calls": MAX_TOOL_CALLS,
            },
        )
        response = self.client.generate(
            role="coverage",
            payload={
                "prompt": prompt,
                "fnol": state["fnol"],
                "fields": state["extracted"],
                "evidence": state.get("evidence", []),
                "tool_history": state.get("tool_history", []),
                "steps": state.get("steps", 0),
                "verification_feedback": state.get("verification_feedback", ""),
            },
            correlation_id=state["correlation_id"],
            prompt_version=version,
        )
        value = response.value
        usage = list(state.get("usage", []))
        usage.append(
            {
                "agent": "coverage",
                "step": state.get("steps", 0) + 1,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cost_usd": response.cost_usd,
                "retries": response.retries,
                "prompt_version": version,
            }
        )
        llm_retries.labels(agent="coverage").inc(response.retries)
        step_latency.labels(agent="coverage", step="decide").observe(time.perf_counter() - started)
        action = value.get("action", "final")
        updated: CoverageState = {
            "steps": state.get("steps", 0) + 1,
            "usage": usage,
            "prompt_version": version,
        }
        if action == "final":
            updated["candidate"] = value
            updated["action"] = "verify"
        elif action in {"search_policy", "get_clause_by_id", "get_clause", "get_fnol_fields"}:
            updated["tool_request"] = {"tool": action, **value}
            updated["action"] = "tool"
        else:
            updated["action"] = "finish"
            updated["error"] = "Agent requested an unavailable tool or invalid action."
        return updated

    def _use_tool(self, state: CoverageState) -> CoverageState:
        request = state.get("tool_request", {})
        tool = request.get("tool")
        if state.get("tool_call_count", 0) >= MAX_TOOL_CALLS:
            return {"action": "finish", "error": "The tool-call limit was reached."}
        started = time.perf_counter()
        result: list[dict[str, Any]] | dict[str, Any] | Any
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                if tool == "search_policy":
                    query = str(request.get("query", ""))[:500]
                    future = executor.submit(self.corpus.search, query, 6)
                    result = future.result(timeout=TOOL_TIMEOUT_SECONDS)
                elif tool in {"get_clause_by_id", "get_clause"}:
                    clause_id = str(request.get("clause_id", ""))
                    future = executor.submit(self.corpus.get_clause, clause_id)
                    result = future.result(timeout=TOOL_TIMEOUT_SECONDS)
                elif tool == "get_fnol_fields":
                    field_name = str(request.get("field_name", ""))
                    future = executor.submit(self.corpus.get_fnol_field, state["extracted"], field_name)
                    result = {"field": field_name, "value": future.result(timeout=TOOL_TIMEOUT_SECONDS)}
                else:
                    raise ValueError(f"Unsupported tool: {tool}")
        except FutureTimeout:
            result = {"error": "tool timeout"}
        except Exception as exc:
            result = {"error": type(exc).__name__}

        evidence = list(state.get("evidence", []))
        if tool == "search_policy" and isinstance(result, list):
            existing = {item["passage_id"] for item in evidence}
            evidence.extend(item for item in result if item["passage_id"] not in existing)
        elif tool in {"get_clause", "get_clause_by_id"} and isinstance(result, dict):
            if result.get("passage_id") and result["passage_id"] not in {item["passage_id"] for item in evidence}:
                evidence.append(result)
        history = list(state.get("tool_history", []))
        history.append(
            {
                "tool": tool,
                "query": request.get("query"),
                "clause_id": request.get("clause_id"),
                "field_name": request.get("field_name"),
                "passage_ids": [row.get("passage_id") for row in result if isinstance(row, dict)]
                if isinstance(result, list)
                else ([result.get("passage_id")] if isinstance(result, dict) and result.get("passage_id") else []),
                "error": result.get("error") if isinstance(result, dict) else None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )
        step_latency.labels(agent="coverage", step=f"tool_{tool or 'unknown'}").observe(time.perf_counter() - started)
        return {
            "evidence": evidence,
            "tool_history": history,
            "tool_call_count": state.get("tool_call_count", 0) + 1,
            "action": "continue",
        }

    def _verify(self, state: CoverageState) -> CoverageState:
        candidate = state.get("candidate", {})
        valid_position = candidate.get("position") in SUPPORTED_POSITIONS
        valid_route = candidate.get("route") in SUPPORTED_ROUTES
        verified, note, overlap = verify_candidate(candidate, state.get("evidence", []))
        verified = verified and valid_position and valid_route
        if not valid_position or not valid_route:
            note = "Output schema contained an unsupported coverage position or route."
        verification_attempts = state.get("verification_attempts", 0) + 1
        tool_calls_so_far = state.get("tool_call_count", 0)
        retrieval_strong = max((row.get("relevance", 0) for row in state.get("evidence", [])), default=0) >= 0.08
        verified = verified and retrieval_strong
        if not retrieval_strong:
            note = "Retrieval was weak; the position is escalated for human review."
        if verified:
            route = candidate["route"]
            if candidate["position"] in ("unclear", "outside_corpus"):
                route = "investigation"
            if state["fnol"].get("injury_reported") or state["extracted"].get("classification") == "total_loss":
                route = "surveyor_assignment"
            resolved_citations = []
            for citation in candidate.get("citations", []):
                passage = self.corpus.get_clause(citation)
                if passage:
                    resolved_citations.append(passage)
            final = {
                "coverage_position": candidate["position"],
                "summary": candidate["summary"],
                "citations": resolved_citations,
                "route": route,
                "route_reason": candidate.get("route_reason", "A claims handler should review the supporting evidence."),
                "escalated": route == "investigation",
                "escalation_path": (
                    "Assigned to an investigation or human coverage review queue."
                    if route == "investigation"
                    else None
                ),
                "verification": {
                    "verified": True,
                    "note": note,
                    "lexical_support": round(overlap, 4),
                    "citations_resolve": True,
                },
            }
            return {"verified": True, "final": final, "verification_attempts": verification_attempts}

        should_retry = verification_attempts < 2 and tool_calls_so_far < MAX_TOOL_CALLS
        if should_retry:
            return {
                "verified": False,
                "verification_feedback": f"{note} Search with a different query before making another attempt.",
                "verification_attempts": verification_attempts,
                "action": "search_again",
                "candidate": {},
            }
        return {
            "verified": False,
            "verification_feedback": note,
            "verification_attempts": verification_attempts,
            "action": "finish",
            "error": note,
        }

    @staticmethod
    def _after_decision(state: CoverageState) -> str:
        if state.get("action") == "tool":
            return "tool"
        if state.get("action") == "verify":
            return "verify"
        return "finish"

    @staticmethod
    def _after_verification(state: CoverageState) -> str:
        if state.get("action") == "search_again":
            return "search_again"
        return "finish"

    def invoke(
        self,
        run_id: str,
        correlation_id: str,
        fnol: dict[str, Any],
        extracted: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.monotonic()
        result = self.graph.invoke(
            {
                "run_id": run_id,
                "correlation_id": correlation_id,
                "fnol": fnol,
                "extracted": extracted,
                "evidence": [],
                "tool_history": [],
                "steps": 0,
                "tool_call_count": 0,
                "started": started,
                "verification_attempts": 0,
                "usage": [],
                "prompt_version": prompt_version(),
            }
        )
        final = result.get("final")
        if final:
            final["metrics"] = {
                "steps": result.get("steps", 0),
                "tool_calls": result.get("tool_call_count", 0),
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "prompt_version": prompt_version(),
                "agent_usage": result.get("usage", []),
            }
            return {
                "final": final,
                "evidence": result.get("evidence", []),
                "tool_history": result.get("tool_history", []),
                "usage": result.get("usage", []),
            }
        return {
            "final": {
                "coverage_position": "unclear",
                "summary": "The system could not verify a policy position from the retrieved passages.",
                "citations": [],
                "route": "investigation",
                "route_reason": result.get("error", "The run did not complete verification."),
                "escalated": True,
                "escalation_path": "Assigned to a human claims handler for policy and evidence review.",
                "verification": {
                    "verified": False,
                    "note": result.get("error", "Bounded reasoning stopped without a verified answer."),
                    "citations_resolve": False,
                },
                "metrics": {
                    "steps": result.get("steps", 0),
                    "tool_calls": result.get("tool_call_count", 0),
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "prompt_version": prompt_version(),
                    "agent_usage": result.get("usage", []),
                },
            },
            "evidence": result.get("evidence", []),
            "tool_history": result.get("tool_history", []),
            "usage": result.get("usage", []),
        }
