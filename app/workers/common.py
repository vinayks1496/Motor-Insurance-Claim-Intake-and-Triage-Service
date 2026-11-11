from __future__ import annotations

import logging
import json
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import session_scope
from app.models import ClaimRun, RunEvent

logger = logging.getLogger(__name__)


def start_heartbeat(agent: str, redis_connection) -> None:
    hostname = socket.gethostname()
    key = f"agent:uptime:{agent}:{hostname}"
    started_at = time.time()

    def beat() -> None:
        while True:
            try:
                redis_connection.set(
                    key,
                    json.dumps(
                        {
                            "agent": agent,
                            "hostname": hostname,
                            "started_at": started_at,
                            "last_seen": time.time(),
                        }
                    ),
                    ex=45,
                )
            except Exception:
                logger.exception("Worker heartbeat could not be stored", extra={"agent": agent})
            time.sleep(10)

    threading.Thread(target=beat, daemon=True, name=f"{agent}-heartbeat").start()


def get_run(run_id: str) -> ClaimRun | None:
    with session_scope() as session:
        return session.get(ClaimRun, run_id)


def update_run(run_id: str, **values: Any) -> None:
    with session_scope() as session:
        run = session.get(ClaimRun, run_id)
        if not run:
            return
        for key, value in values.items():
            setattr(run, key, value)
        run.updated_at = datetime.now(timezone.utc)


def record_event(
    run_id: str,
    correlation_id: str,
    agent: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    prompt_version: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: float = 0.0,
) -> None:
    with session_scope() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                correlation_id=correlation_id,
                agent=agent,
                event_type=event_type,
                payload=payload or {},
                prompt_version=prompt_version,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
            )
        )


def run_snapshot(run_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        run = session.get(ClaimRun, run_id)
        if not run:
            return None
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id)
        ).all()
        return {
            "id": run.id,
            "correlation_id": run.correlation_id,
            "status": run.status,
            "fnol": run.fnol,
            "intake": run.intake,
            "result": run.result,
            "created_at": run.created_at,
            "events": events,
        }


def finalize_run(
    run_id: str,
    correlation_id: str,
    result: dict[str, Any],
    *,
    status: str,
    agent: str,
) -> float:
    finished = datetime.now(timezone.utc)
    with session_scope() as session:
        run = session.get(ClaimRun, run_id)
        if not run:
            return 0.0
        run.result = result
        run.status = status
        run.finished_at = finished
        run.updated_at = finished
        started = run.created_at
    return max(0.0, (finished - started).total_seconds())


def total_usage(usage: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "input_tokens": sum(int(item.get("input_tokens", 0)) for item in usage),
        "output_tokens": sum(int(item.get("output_tokens", 0)) for item in usage),
        "cost_usd": round(sum(float(item.get("cost_usd", 0)) for item in usage), 8),
        "calls": len(usage),
    }
