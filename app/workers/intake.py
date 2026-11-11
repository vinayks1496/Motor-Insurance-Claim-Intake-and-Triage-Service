from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import redis
from opentelemetry import trace
from rq import Queue, Retry, Worker

from app.agents.intake_agent import IntakeAgent
from app.config import redis_url
from app.database import initialize_database
from app.guards import inspect_text, safe_free_text
from app.llm import ProviderUnavailable
from app.logging_config import configure_logging
from app.metrics import agent_tasks, completed_runs, llm_retries, run_latency, start_worker_metrics, step_latency
from app.telemetry import configure_telemetry
from app.workers.common import finalize_run, get_run, record_event, start_heartbeat, update_run

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("claimflow.intake")


def _escalate(run_id: str, correlation_id: str, reason: str, flags: dict[str, bool] | None = None) -> dict[str, Any]:
    result = {
        "coverage_position": "unclear",
        "summary": "The request needs human review before a policy position can be formed.",
        "citations": [],
        "route": "investigation",
        "route_reason": reason,
        "escalated": True,
        "escalation_path": "Assigned to a human claims handler; the automated run stopped safely.",
        "verification": {"verified": False, "note": reason, "citations_resolve": False},
        "prompt_version": "intake-v1",
        "metrics": {"steps": 0, "tool_calls": 0, "latency_ms": 0, "agent_usage": []},
        "guardrail_flags": flags or {},
    }
    elapsed = finalize_run(run_id, correlation_id, result, status="escalated", agent="intake")
    run_latency.observe(elapsed)
    completed_runs.labels(status="escalated").inc()
    return result


def process_intake(run_id: str) -> None:
    snapshot = get_run(run_id)
    if not snapshot or snapshot.status in ("completed", "escalated", "quarantined"):
        return
    correlation_id = snapshot.correlation_id
    started = time.perf_counter()
    fnol = dict(snapshot.fnol)
    logger.info("Intake agent started", extra={"run_id": run_id, "correlation_id": correlation_id, "agent": "intake"})
    update_run(run_id, status="intake_in_progress", started_at=datetime.now(timezone.utc))
    record_event(run_id, correlation_id, "intake", "agent_started", {"role": "classification_and_extraction"})

    text = safe_free_text(str(fnol.get("loss_description", "")))
    flags = inspect_text(text)
    if flags["prompt_injection"] or flags["toxicity"]:
        reason = (
            "Untrusted free text triggered the prompt-injection guard."
            if flags["prompt_injection"]
            else "Input toxicity detection requires a human review."
        )
        record_event(
            run_id,
            correlation_id,
            "guardrail",
            "input_blocked",
            {"flags": flags, "model_received_free_text": False},
        )
        agent_tasks.labels(agent="intake", result="guardrail_escalated").inc()
        _escalate(run_id, correlation_id, reason, flags)
        return

    fnol["loss_description"] = text
    try:
        with tracer.start_as_current_span("intake_agent") as span:
            span.set_attribute("claimflow.run_id", run_id)
            span.set_attribute("claimflow.correlation_id", correlation_id)
            span.set_attribute("claimflow.agent", "intake")
            result = IntakeAgent().invoke(fnol, correlation_id)["result"]
        usage = result.pop("_usage", {})
        elapsed = time.perf_counter() - started
        step_latency.labels(agent="intake", step="classify_extract").observe(elapsed)
        llm_retries.labels(agent="intake").inc(int(usage.get("retries", 0)))
        record_event(
            run_id,
            correlation_id,
            "intake",
            "llm_call",
            {
                "classification": result.get("classification"),
                "entities": result.get("entities"),
                "retries": int(usage.get("retries", 0)),
            },
            prompt_version=usage.get("prompt_version", "intake-v1"),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cost_usd=float(usage.get("cost_usd", 0)),
            latency_ms=elapsed * 1000,
        )
        update_run(run_id, intake=result, status="coverage_pending")
        record_event(
            run_id,
            correlation_id,
            "intake",
            "handoff",
            {"to": "coverage", "classification": result.get("classification")},
            prompt_version="intake-v1",
        )
        agent_tasks.labels(agent="intake", result="success").inc()
        connection = redis.Redis.from_url(redis_url())
        queue = Queue("coverage", connection=connection, default_timeout=120)
        queue.enqueue(
            "app.workers.coverage.process_coverage",
            run_id,
            retry=Retry(max=3, interval=[1, 2, 4]),
            job_timeout=120,
            result_ttl=3600,
            failure_ttl=86400,
        )
    except ProviderUnavailable as exc:
        logger.warning(
            "Intake provider unavailable; using human escalation fallback",
            extra={"run_id": run_id, "correlation_id": correlation_id, "agent": "intake"},
        )
        record_event(run_id, correlation_id, "intake", "provider_failure", {"reason": str(exc)})
        agent_tasks.labels(agent="intake", result="provider_failure").inc()
        _escalate(
            run_id,
            correlation_id,
            "The intake provider did not complete after bounded retries; the claim is not treated as a confident answer.",
        )
    except Exception as exc:
        logger.exception(
            "Intake agent failed",
            extra={"run_id": run_id, "correlation_id": correlation_id, "agent": "intake"},
        )
        record_event(run_id, correlation_id, "intake", "agent_failure", {"reason": type(exc).__name__})
        agent_tasks.labels(agent="intake", result="failure").inc()
        _escalate(run_id, correlation_id, "The intake agent failed before handing off; human review is required.")


def main() -> None:
    configure_logging()
    configure_telemetry("claimflow-intake-worker")
    initialize_database()
    start_worker_metrics(9101)
    connection = redis.Redis.from_url(redis_url())
    start_heartbeat("intake", connection)
    worker = Worker([Queue("intake", connection=connection)], connection=connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
