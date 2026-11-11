from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "cases.json"
RESULTS_DIR = ROOT / "results"
TERMINAL = {"completed", "escalated", "quarantined"}


def payload_for(case: dict[str, Any], index: int) -> dict[str, Any]:
    payload = {
        "client_reference": f"EVAL-{index:03d}-{case['id'][:44]}",
        "policy_number": f"EVAL/2026/{index:04d}",
        "insurer": case.get("insurer", "New India Assurance"),
        "product": case.get("product", "Private Car Base Package Policy"),
        "registration_number": f"KA03MN{index:04d}",
        "loss_date": "2026-05-10",
        "reported_at": "2026-05-10",
        "loss_description": case["description"],
        "loss_kind": case["loss_kind"],
        "vehicle_use": case["vehicle_use"],
        "channel": "web",
        "estimate_inr": case.get("estimate_inr"),
        "injury_reported": bool(case.get("injury_reported", False)),
        "injury_count": int(case.get("injury_count", 0)),
        "police_reported": bool(case.get("police_reported", False)),
    }
    return payload


def run_case(client: httpx.Client, base_url: str, case: dict[str, Any], index: int, timeout: int) -> dict[str, Any]:
    submission = client.post(
        f"{base_url}/api/v1/runs",
        json=payload_for(case, index),
        headers={"Idempotency-Key": f"evaluation-{case['id']}-{uuid4()}"},
    )
    submission.raise_for_status()
    run_id = submission.json()["run_id"]
    deadline = time.monotonic() + timeout
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"{base_url}/api/v1/runs/{run_id}")
        response.raise_for_status()
        detail = response.json()
        if detail.get("status") in TERMINAL:
            break
        time.sleep(1.5)
    else:
        raise TimeoutError(f"run {run_id} did not reach a terminal state within {timeout}s")

    actual = detail.get("result") or {}
    intake = detail.get("intake") or {}
    guardrail_flags = actual.get("guardrail_flags") or {}
    expected_position = case["expected_position"]
    expected_route = case["expected_route"]
    expected_escalated = bool(case["expected_escalated"])
    expected_classification = case.get("expected_classification")
    verified = bool((actual.get("verification") or {}).get("verified"))
    citations = actual.get("citations") or []
    citation_ids = [row.get("passage_id") for row in citations if isinstance(row, dict)]
    position_match = actual.get("coverage_position") == expected_position
    route_match = actual.get("route") == expected_route
    escalation_match = (detail.get("status") == "escalated") == expected_escalated
    classification_match = expected_classification is None or intake.get("classification") == expected_classification
    guardrail_name = case.get("expected_guardrail")
    guardrail_match = not guardrail_name or bool(guardrail_flags.get(guardrail_name))
    citation_integrity = (not verified) or bool(citation_ids and len(citation_ids) == len(set(citation_ids)))
    return {
        "case_id": case["id"],
        "run_id": run_id,
        "status": detail.get("status"),
        "expected_position": expected_position,
        "actual_position": actual.get("coverage_position"),
        "position_match": position_match,
        "expected_route": expected_route,
        "actual_route": actual.get("route"),
        "route_match": route_match,
        "expected_escalated": expected_escalated,
        "actual_escalated": detail.get("status") == "escalated",
        "escalation_match": escalation_match,
        "expected_classification": expected_classification,
        "actual_classification": intake.get("classification"),
        "classification_match": classification_match,
        "guardrail_match": guardrail_match,
        "verified": verified,
        "citation_integrity": citation_integrity,
        "citation_count": len(citation_ids),
        "latency_seconds": round((datetime.fromisoformat(detail["finished_at"]) - datetime.fromisoformat(detail["created_at"])).total_seconds(), 3),
        "cost_usd": float((actual.get("usage") or {}).get("cost_usd", 0.0)),
    }


def run_quarantine_check(client: httpx.Client, base_url: str) -> dict[str, Any]:
    invalid = {
        "client_reference": "EVAL-QUARANTINE",
        "unexpected_field": "this field must be rejected",
    }
    response = client.post(f"{base_url}/api/v1/runs", json=invalid)
    body = response.json()
    return {
        "status_code": response.status_code,
        "quarantine_id": body.get("quarantine_id"),
        "agent_run_started": body.get("agent_run_started"),
        "passed": response.status_code == 422 and body.get("agent_run_started") is False and bool(body.get("quarantine_id")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the labeled 20-case ClaimFlow advisory evaluation.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout-seconds", type=int, default=150)
    parser.add_argument("--submission-delay-seconds", type=float, default=8.2, help="Keeps the default per-client API limit intact.")
    parser.add_argument("--skip-quarantine-check", action="store_true")
    args = parser.parse_args()

    suite = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    results = []
    with httpx.Client(timeout=20) as client:
        health = client.get(f"{args.base_url}/health/ready")
        health.raise_for_status()
        for index, case in enumerate(suite["cases"], start=1):
            print(f"[{index}/{len(suite['cases'])}] {case['id']}", flush=True)
            results.append(run_case(client, args.base_url, case, index, args.timeout_seconds))
            if index < len(suite["cases"]) and args.submission_delay_seconds > 0:
                time.sleep(args.submission_delay_seconds)
        quarantine = None if args.skip_quarantine_check else run_quarantine_check(client, args.base_url)

    dimensions = ["position_match", "route_match", "escalation_match", "classification_match", "guardrail_match", "citation_integrity"]
    summary = {name: round(sum(bool(row[name]) for row in results) / max(1, len(results)), 4) for name in dimensions}
    summary["cases"] = len(results)
    summary["mean_latency_seconds"] = round(sum(row["latency_seconds"] for row in results) / max(1, len(results)), 3)
    summary["mean_cost_usd"] = round(sum(row["cost_usd"] for row in results) / max(1, len(results)), 8)
    summary["verified_answer_rate"] = round(sum(row["verified"] for row in results) / max(1, len(results)), 4)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "suite": suite["suite"],
        "started_at": timestamp,
        "base_url": args.base_url,
        "summary": summary,
        "quarantine_check": quarantine,
        "results": results,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output = RESULTS_DIR / f"evaluation-{timestamp}.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    (RESULTS_DIR / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    csv_path = RESULTS_DIR / f"evaluation-{timestamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(json.dumps({"summary": summary, "quarantine_check": quarantine, "json": str(output), "csv": str(csv_path)}, indent=2))


if __name__ == "__main__":
    main()
