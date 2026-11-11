from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import redis
from opentelemetry import trace
from rq import Queue, Retry, Worker

from app.agents.coverage_agent import CoverageAgent
from app.config import redis_url
from app.database import initialize_database
from app.logging_config import configure_logging
from app.metrics import agent_tasks, completed_runs, run_latency, start_worker_metrics
from app.quality import sample_this_run, score_live_run
from app.telemetry import configure_telemetry
from app.workers.common import (
    finalize_run,
    get_run,
    record_event,
    start_heartbeat,
    total_usage,
    update_run,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("claimflow.coverage")


def process_coverage(run_id: str) -> None:
    snapshot = get_run(run_id)
    if not snapshot or snapshot.status in ("completed", "escalated", "quarantined"):
        return
    correlation_id = snapshot.correlation_id
    if not snapshot.intake:
        _finalize_escalation(run_id, correlation_id, "The intake-to-coverage handoff did not include extracted fields.")
        return
    update_run(run_id, status="coverage_in_progress")
    record_event(run_id, correlation_id, "coverage", "agent_started", {"role": "policy_analysis"})
    try:
        with tracer.start_as_current_span("coverage_agent") as span:
            span.set_attribute("claimflow.run_id", run_id)
            span.set_attribute("claimflow.correlation_id", correlation_id)
            span.set_attribute("claimflow.agent", "coverage")
            result = CoverageAgent().invoke(
                run_id=run_id,
                correlation_id=correlation_id,
                fnol=snapshot.fnol,
                extracted=snapshot.intake,
            )
        final = result["final"]
        usage = result.get("usage", [])
        for index, call in enumerate(usage, start=1):
            record_event(
                run_id,
                correlation_id,
                "coverage",
                "llm_call",
                {"step": call.get("step", index), "retries": int(call.get("retries", 0))},
                prompt_version=call.get("prompt_version"),
                input_tokens=int(call.get("input_tokens", 0)),
                output_tokens=int(call.get("output_tokens", 0)),
                cost_usd=float(call.get("cost_usd", 0)),
            )
        for entry in result.get("tool_history", []):
            record_event(
                run_id,
                correlation_id,
                "coverage",
                "tool_call",
                entry,
                latency_ms=float(entry.get("latency_ms", 0)),
            )
        record_event(
            run_id,
            correlation_id,
            "coverage",
            "verification",
            final.get("verification", {}),
            prompt_version=final.get("metrics", {}).get("prompt_version"),
        )
        final["classification"] = snapshot.intake.get("classification")
        final["entities"] = snapshot.intake.get("entities", {})
        final["prompt_version"] = final.get("metrics", {}).get("prompt_version")
        final["usage"] = total_usage(usage)
        final["correlation_id"] = correlation_id
        final["timeline"] = result.get("tool_history", [])
        if sample_this_run(run_id):
            quality = score_live_run(final, result.get("evidence", []))
            from app.database import session_scope
            from app.models import QualitySample

            with session_scope() as session:
                session.add(QualitySample(run_id=run_id, **quality))
            record_event(run_id, correlation_id, "quality", "sample_scored", quality)
        # Completed means automated processing produced a verified result.
        # A verified result can still be routed to investigation for a handler.
        status = "completed" if final.get("verification", {}).get("verified") else "escalated"
        elapsed = finalize_run(run_id, correlation_id, final, status=status, agent="coverage")
        run_latency.observe(elapsed)
        completed_runs.labels(status=status).inc()
        agent_tasks.labels(agent="coverage", result="success" if status == "completed" else "escalated").inc()
        record_event(
            run_id,
            correlation_id,
            "coverage",
            "run_finalized",
            {
                "status": status,
                "route": final.get("route"),
                "coverage_position": final.get("coverage_position"),
                "end_to_end_seconds": round(elapsed, 3),
                "cost_per_100_runs_usd": round(final["usage"]["cost_usd"] * 100, 6),
            },
        )
    except Exception as exc:
        logger.exception(
            "Coverage agent failed; falling back to human review",
            extra={"run_id": run_id, "correlation_id": correlation_id, "agent": "coverage"},
        )
        record_event(
            run_id,
            correlation_id,
            "coverage",
            "agent_failure",
            {"reason": type(exc).__name__, "confident_answer_returned": False},
        )
        agent_tasks.labels(agent="coverage", result="failure").inc()
        _finalize_escalation(
            run_id,
            correlation_id,
            "The coverage agent failed or its provider was unavailable; no unverified position was returned.",
        )


def _finalize_escalation(run_id: str, correlation_id: str, reason: str) -> None:
    result: dict[str, Any] = {
        "coverage_position": "unclear",
        "summary": "The automated review could not support a verified position.",
        "citations": [],
        "route": "investigation",
        "route_reason": reason,
        "escalated": True,
        "escalation_path": "Assigned to a human claims handler for review.",
        "verification": {"verified": False, "note": reason, "citations_resolve": False},
        "correlation_id": correlation_id,
        "prompt_version": "coverage-v2",
        "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0},
    }
    elapsed = finalize_run(run_id, correlation_id, result, status="escalated", agent="coverage")
    run_latency.observe(elapsed)
    completed_runs.labels(status="escalated").inc()
    record_event(run_id, correlation_id, "coverage", "run_escalated", {"reason": reason})


def main() -> None:
    configure_logging()
    configure_telemetry("claimflow-coverage-worker")
    initialize_database()
    start_worker_metrics(9102)
    connection = redis.Redis.from_url(redis_url())
    start_heartbeat("coverage", connection)
    worker = Worker([Queue("coverage", connection=connection)], connection=connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
