from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from uuid import UUID, uuid4

import redis
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from rq import Queue, Retry
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

from app.config import corpus_dir, integer, redis_url
from app.contracts import DemoOutageRequest, FeedbackRequest, FNOLRequest
from app.database import SessionLocal, engine, initialize_database, session_scope
from app.guards import inspect_text, safe_free_text
from app.logging_config import configure_logging
from app.metrics import (
    completed_runs,
    feedback_records,
    http_requests,
    quarantined_requests,
    queue_depth,
    run_latency,
    submitted_runs,
)
from app.models import ClaimRun, FeedbackRecord, QualitySample, QuarantineRecord, RunEvent
from app.telemetry import configure_telemetry
from app.workers.common import record_event

configure_logging()
configure_telemetry("claimflow-api")
logger = logging.getLogger(__name__)
redis_client = redis.Redis.from_url(redis_url(), decode_responses=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine)
    yield


app = FastAPI(
    title="ClaimFlow advisory claim intake and triage",
    version="1.0.0",
    description=(
        "A human-reviewed recommendation service. It never approves or rejects a claim. "
        "Submit an FNOL, poll the returned run identifier, and review clause-level citations."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def correlation_and_metrics(request: Request, call_next):
    raw = request.headers.get("X-Correlation-ID")
    try:
        correlation_id = str(UUID(raw)) if raw else str(uuid4())
    except ValueError:
        correlation_id = str(uuid4())
    request.state.correlation_id = correlation_id
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    response.headers["X-Correlation-ID"] = correlation_id
    http_requests.labels(request.method, request.url.path, str(response.status_code)).inc()
    if request.url.path.startswith("/api/v1/runs") and request.method == "POST" and response.status_code == 202:
        logger.info("Accepted asynchronous FNOL run", extra={"correlation_id": correlation_id})
    response.headers["X-Response-Time-Ms"] = str(round(elapsed * 1000, 2))
    return response


def _error_rows(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "field": ".".join(str(item) for item in error.get("loc", []) if item != "body") or "body",
            "message": str(error.get("msg", "invalid value")),
            "code": str(error.get("type", "validation_error")),
        }
        for error in errors
    ]


def _redacted_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"policy_number", "registration_number"}:
                redacted[key] = "[redacted]"
            elif lowered in {"loss_description", "description"} and isinstance(item, str):
                redacted[key] = item[:120]
            else:
                redacted[key] = _redacted_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redacted_payload(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:2000]
    return value


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    correlation_id = getattr(request.state, "correlation_id", str(uuid4()))
    errors = _error_rows(exc.errors())
    try:
        raw = await request.body()
        payload = _redacted_payload(json.loads(raw.decode("utf-8"))) if raw else None
    except Exception:
        payload = {"body": "[invalid JSON or unreadable payload]"}
    try:
        with session_scope() as session:
            row = QuarantineRecord(
                correlation_id=correlation_id,
                field_errors=errors,
                payload_excerpt=payload,
                agent_run_started=False,
            )
            session.add(row)
            session.flush()
            quarantine_id = row.id
    except Exception:
        logger.exception("Could not persist quarantine record", extra={"correlation_id": correlation_id})
        quarantine_id = None
    quarantined_requests.inc()
    logger.info(
        "Malformed request quarantined before agent execution",
        extra={"correlation_id": correlation_id, "agent": "ingestion"},
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "request_validation_failed",
            "correlation_id": correlation_id,
            "quarantine_id": quarantine_id,
            "field_errors": errors,
            "agent_run_started": False,
        },
    )


def _request_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Client-IP")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request) -> None:
    client_hash = hashlib.sha256(_request_ip(request).encode()).hexdigest()[:24]
    key = f"rate:fnol:{client_hash}:{int(time.time() // 60)}"
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, 120)
    limit = integer("CLIENT_RATE_LIMIT_PER_MINUTE", 8)
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail={"error": "client_rate_limit_exceeded", "retry_after_seconds": 60},
            headers={"Retry-After": "60"},
        )


def _dump_run(run: ClaimRun, include_events: bool = False) -> dict[str, Any]:
    data = {
        "run_id": run.id,
        "correlation_id": run.correlation_id,
        "status": run.status,
        "fnol": run.fnol,
        "intake": run.intake,
        "result": run.result,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    if include_events:
        events = []
        with session_scope() as session:
            rows = session.scalars(
                select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.id)
            ).all()
            for row in rows:
                events.append(
                    {
                        "id": row.id,
                        "agent": row.agent,
                        "event_type": row.event_type,
                        "payload": row.payload,
                        "prompt_version": row.prompt_version,
                        "input_tokens": row.input_tokens,
                        "output_tokens": row.output_tokens,
                        "cost_usd": row.cost_usd,
                        "latency_ms": row.latency_ms,
                        "created_at": row.created_at.isoformat(),
                    }
                )
        data["events"] = events
    return data


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        redis_client.ping()
        return {"status": "ready", "database": "ok", "queue": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "reason": type(exc).__name__})


@app.get("/metrics")
def prometheus_metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/v1/contracts")
def contracts() -> dict[str, Any]:
    return {
        "submit": {
            "method": "POST",
            "path": "/api/v1/runs",
            "success_status": 202,
            "idempotency_header": "Idempotency-Key (optional; client_reference is the fallback)",
            "response": {"run_id": "UUID", "correlation_id": "UUID", "status": "queued", "poll": "/api/v1/runs/{run_id}"},
            "schema": FNOLRequest.model_json_schema(),
        },
        "feedback": {
            "method": "POST",
            "path": "/api/v1/runs/{run_id}/feedback",
            "schema": FeedbackRequest.model_json_schema(),
        },
    }


@app.post("/api/v1/runs", status_code=status.HTTP_202_ACCEPTED)
def submit_fnol(
    body: FNOLRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _rate_limit(request)
    correlation_id = request.state.correlation_id
    client_hash = hashlib.sha256(_request_ip(request).encode()).hexdigest()[:20]
    raw_key = (idempotency_key or f"client:{body.client_reference}").strip()
    key = f"{client_hash}:{raw_key}"[:128]
    if not key:
        raise HTTPException(status_code=422, detail="Idempotency-Key cannot be empty")

    with session_scope() as session:
        existing = session.scalar(select(ClaimRun).where(ClaimRun.idempotency_key == key))
        if existing:
            return {
                "run_id": existing.id,
                "correlation_id": existing.correlation_id,
                "status": existing.status,
                "idempotent_replay": True,
            }

    fnol = body.model_dump(mode="json")
    fnol["loss_description"] = safe_free_text(fnol["loss_description"])
    guard_flags = inspect_text(fnol["loss_description"])
    fnol["_guardrails"] = guard_flags
    run_id = str(uuid4())
    run = ClaimRun(
        id=run_id,
        correlation_id=correlation_id,
        idempotency_key=key,
        status="queued",
        fnol=fnol,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    try:
        with session_scope() as session:
            session.add(run)
            session.flush()
    except IntegrityError:
        with session_scope() as session:
            existing = session.scalar(select(ClaimRun).where(ClaimRun.idempotency_key == key))
            if existing:
                return {
                    "run_id": existing.id,
                    "correlation_id": existing.correlation_id,
                    "status": existing.status,
                    "idempotent_replay": True,
                }
        raise

    try:
        queue = Queue("intake", connection=redis_client, default_timeout=120)
        queue.enqueue(
            "app.workers.intake.process_intake",
            run_id,
            retry=Retry(max=3, interval=[1, 2, 4]),
            job_timeout=120,
            result_ttl=3600,
            failure_ttl=86400,
        )
        queue_depth.labels(queue="intake").set(queue.count)
    except Exception as exc:
        with session_scope() as session:
            current = session.get(ClaimRun, run_id)
            if current:
                current.status = "escalated"
                current.finished_at = datetime.now(timezone.utc)
                current.result = {
                    "coverage_position": "unclear",
                    "summary": "The request could not enter the processing queue.",
                    "citations": [],
                    "route": "investigation",
                    "route_reason": "The queue is unavailable; a human handler must review this request.",
                    "escalated": True,
                    "escalation_path": "Contact the claims handling team and retry with the run identifier.",
                    "verification": {"verified": False, "note": "Queue unavailable."},
                }
        logger.exception("Queue handoff failed", extra={"run_id": run_id, "correlation_id": correlation_id})
        raise HTTPException(status_code=503, detail={"error": "queue_unavailable", "run_id": run_id}) from exc

    submitted_runs.inc()
    record_event(
        run_id,
        correlation_id,
        "ingestion",
        "request_accepted",
        {"guardrail_flags": guard_flags, "agent_run_started": False},
    )
    return {
        "run_id": run_id,
        "correlation_id": correlation_id,
        "status": "queued",
        "idempotent_replay": False,
        "poll": f"/api/v1/runs/{run_id}",
    }


@app.get("/api/v1/runs")
def list_runs(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    with session_scope() as session:
        runs = session.scalars(select(ClaimRun).order_by(desc(ClaimRun.created_at)).limit(limit)).all()
        return {"runs": [_dump_run(run) for run in runs]}


@app.get("/api/v1/runs/{run_id}")
def get_run_detail(run_id: str) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(ClaimRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        return _dump_run(run, include_events=True)


@app.get("/api/v1/quarantine")
def list_quarantine(limit: int = 25) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    with session_scope() as session:
        rows = session.scalars(
            select(QuarantineRecord).order_by(desc(QuarantineRecord.created_at)).limit(limit)
        ).all()
        return {
            "records": [
                {
                    "id": row.id,
                    "correlation_id": row.correlation_id,
                    "field_errors": row.field_errors,
                    "payload_excerpt": row.payload_excerpt,
                    "agent_run_started": row.agent_run_started,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }


@app.post("/api/v1/runs/{run_id}/feedback", status_code=201)
def submit_feedback(run_id: str, body: FeedbackRequest) -> dict[str, Any]:
    with session_scope() as session:
        run = session.get(ClaimRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        feedback = FeedbackRecord(
            run_id=run_id,
            satisfactory=body.satisfactory,
            reason=body.reason,
        )
        session.add(feedback)
        session.flush()
        feedback_id = feedback.id
    feedback_records.labels(satisfactory=str(body.satisfactory).lower()).inc()
    record_event(
        run_id,
        run.correlation_id,
        "feedback",
        "feedback_received",
        {"satisfactory": body.satisfactory, "reason": body.reason},
    )
    return {"feedback_id": feedback_id, "run_id": run_id, "stored": True}


@app.get("/api/v1/feedback/summary")
def feedback_summary() -> dict[str, Any]:
    with session_scope() as session:
        total = session.scalar(select(func.count()).select_from(FeedbackRecord)) or 0
        yes = session.scalar(
            select(func.count()).select_from(FeedbackRecord).where(FeedbackRecord.satisfactory.is_(True))
        ) or 0
        no = total - yes
        return {"total": total, "satisfactory": yes, "unsatisfactory": no}


@app.get("/api/v1/feedback/recent")
def recent_feedback(limit: int = 25) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    with session_scope() as session:
        rows = session.scalars(
            select(FeedbackRecord).order_by(desc(FeedbackRecord.created_at)).limit(limit)
        ).all()
        return {
            "records": [
                {
                    "id": row.id,
                    "run_id": row.run_id,
                    "satisfactory": row.satisfactory,
                    "reason": row.reason,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }


@app.get("/api/v1/quality/trend")
def quality_trend(limit: int = 60) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    with session_scope() as session:
        rows = session.scalars(
            select(QualitySample).order_by(desc(QualitySample.created_at)).limit(limit)
        ).all()
        rows.reverse()
        return {
            "samples": [
                {
                    "run_id": row.run_id,
                    "groundedness": row.groundedness,
                    "context_relevance": row.context_relevance,
                    "faithfulness": row.faithfulness,
                    "hallucination_rate": row.hallucination_rate,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }


@app.get("/api/v1/dashboard")
def dashboard_data() -> dict[str, Any]:
    with session_scope() as session:
        runs = session.scalars(select(ClaimRun).order_by(desc(ClaimRun.created_at)).limit(100)).all()
        statuses = {
            "queued": 0,
            "intake_in_progress": 0,
            "coverage_pending": 0,
            "coverage_in_progress": 0,
            "completed": 0,
            "escalated": 0,
            "quarantined": 0,
        }
        durations = []
        costs = []
        route_counts: dict[str, int] = {}
        total_steps = total_tools = total_retries = calls = 0
        for run in runs:
            status_key = run.status
            statuses[status_key] = statuses.get(status_key, 0) + 1
            if run.finished_at:
                durations.append(max(0, (run.finished_at - run.created_at).total_seconds()))
            if run.result:
                route = run.result.get("route", "investigation")
                route_counts[route] = route_counts.get(route, 0) + 1
                metrics = run.result.get("metrics", {})
                total_steps += int(metrics.get("steps", 0))
                total_tools += int(metrics.get("tool_calls", 0))
                usage = run.result.get("usage", {})
                costs.append(float(usage.get("cost_usd", 0)))
                calls += int(usage.get("calls", 0))
        feedback_total = session.scalar(select(func.count()).select_from(FeedbackRecord)) or 0
        feedback_yes = session.scalar(
            select(func.count()).select_from(FeedbackRecord).where(FeedbackRecord.satisfactory.is_(True))
        ) or 0
        feedback_no = feedback_total - feedback_yes
        quality_count = session.scalar(select(func.count()).select_from(QualitySample)) or 0
        quality_rows = session.scalars(select(QualitySample).order_by(desc(QualitySample.created_at)).limit(10)).all()
        quality = [
            {
                "groundedness": row.groundedness,
                "context_relevance": row.context_relevance,
                "faithfulness": row.faithfulness,
                "hallucination_rate": row.hallucination_rate,
                "created_at": row.created_at.isoformat(),
            }
            for row in reversed(quality_rows)
        ]
        recent = [_dump_run(run) for run in runs[:12]]
        quarantine_count = session.scalar(select(func.count()).select_from(QuarantineRecord)) or 0
        run_ids = [run.id for run in runs]
        events = (
            session.scalars(select(RunEvent).where(RunEvent.run_id.in_(run_ids))).all()
            if run_ids
            else []
        )

    try:
        intake_queue = Queue("intake", connection=redis_client).count
        coverage_queue = Queue("coverage", connection=redis_client).count
    except Exception:
        intake_queue = coverage_queue = 0
    events_by_agent: dict[str, dict[str, int]] = {}
    total_retries = llm_calls = 0
    for event in events:
        bucket = events_by_agent.setdefault(event.agent, {"started": 0, "successful": 0})
        if event.event_type == "agent_started":
            bucket["started"] += 1
        if event.event_type == "handoff" or (
            event.event_type == "verification" and (event.payload or {}).get("verified") is True
        ):
            bucket["successful"] += 1
        if event.event_type == "llm_call":
            llm_calls += 1
            total_retries += int((event.payload or {}).get("retries", 0))
    agent_rates = {
        agent: round(counts["successful"] / max(1, counts["started"]), 4)
        for agent, counts in events_by_agent.items()
    }
    uptime_hosts = []
    now_epoch = time.time()
    try:
        for key_name in redis_client.scan_iter("agent:uptime:*"):
            value = redis_client.get(key_name)
            if not value:
                continue
            record = json.loads(value)
            uptime_hosts.append(
                {
                    "agent": record.get("agent"),
                    "host": record.get("hostname"),
                    "uptime_seconds": round(max(0, now_epoch - float(record.get("started_at", now_epoch))), 1),
                    "last_seen_seconds": round(max(0, now_epoch - float(record.get("last_seen", now_epoch))), 1),
                }
            )
    except Exception:
        uptime_hosts = []
    all_durations = sorted(durations)
    p50 = median(all_durations) if all_durations else 0.0
    p95 = all_durations[min(len(all_durations) - 1, int(len(all_durations) * 0.95))] if all_durations else 0.0
    average_cost = sum(costs) / max(1, len(costs))
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "slo": {"p95_seconds": integer("RUN_SLO_P95_SECONDS", 15), "measurement": "submission to final result"},
        "totals": {
            "runs_in_sample": len(runs),
            "queued": sum(statuses[key] for key in ("queued", "intake_in_progress", "coverage_pending", "coverage_in_progress")) + intake_queue + coverage_queue,
            "completed": statuses["completed"],
            "escalated": statuses["escalated"],
            "quarantined": quarantine_count,
            "routes": route_counts,
        },
        "performance": {
            "p50_seconds": round(p50, 2),
            "p95_seconds": round(p95, 2),
            "slo_met": bool(all_durations and p95 <= integer("RUN_SLO_P95_SECONDS", 15)),
            "mean_steps_per_run": round(total_steps / max(1, len(durations)), 2),
            "mean_tool_calls_per_run": round(total_tools / max(1, len(durations)), 2),
            "retry_rate": round(total_retries / max(1, llm_calls), 4),
            "agent_success_rate": agent_rates,
            "worker_uptime": uptime_hosts,
        },
        "cost": {
            "mean_usd_per_run": round(average_cost, 8),
            "usd_per_100_runs": round(average_cost * 100, 6),
            "largest_single_call_usd": max(costs, default=0.0),
        },
        "feedback": {
            "total": feedback_total,
            "satisfactory": feedback_yes,
            "unsatisfactory": feedback_no,
        },
        "quality": {"sample_count": quality_count, "trend": quality},
        "recent_runs": recent,
    }


@app.get("/api/v1/corpus/status")
def corpus_status() -> dict[str, Any]:
    import os
    from pathlib import Path

    manifest_path = Path(os.getenv("INDEX_DIR", "data/index")) / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=503, detail="corpus index is not ready")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@app.get("/api/v1/corpus/sources")
def corpus_sources() -> dict[str, Any]:
    source_path = Path(corpus_dir()) / "sources.json"
    if not source_path.exists():
        raise HTTPException(status_code=503, detail="corpus source manifest is unavailable")
    return json.loads(source_path.read_text(encoding="utf-8"))


@app.get("/api/v1/evaluation/summary")
def evaluation_summary() -> dict[str, Any]:
    cases_path = Path("evaluation/cases.json")
    results_path = Path("evaluation/results/latest.json")
    if not cases_path.exists():
        return {"status": "unavailable", "case_count": 0, "latest": None}
    suite = json.loads(cases_path.read_text(encoding="utf-8"))
    latest = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else None
    return {
        "status": "complete" if latest else "not_run",
        "suite": suite.get("suite"),
        "case_count": len(suite.get("cases", [])),
        "case_definitions": [
            {
                "id": case.get("id"),
                "expected_position": case.get("expected_position"),
                "expected_route": case.get("expected_route"),
                "expected_escalated": case.get("expected_escalated"),
            }
            for case in suite.get("cases", [])
        ],
        "latest": latest,
    }


@app.post("/api/v1/demo/provider-outage")
def demo_provider_outage(body: DemoOutageRequest) -> dict[str, Any]:
    if body.enabled:
        redis_client.setex("demo:provider-outage", body.duration_seconds, "1")
        redis_client.delete("llm:circuit:failures", "llm:circuit:open")
    else:
        redis_client.delete("demo:provider-outage", "llm:circuit:failures", "llm:circuit:open")
    return {
        "enabled": body.enabled,
        "duration_seconds": body.duration_seconds if body.enabled else 0,
        "circuit_state": "closed; failure threshold opens it after three failed calls",
        "fallback": "human investigation escalation; no unverified answer",
    }


@app.get("/api/v1/cost/summary")
def cost_summary() -> dict[str, Any]:
    data = dashboard_data()
    return data["cost"]
