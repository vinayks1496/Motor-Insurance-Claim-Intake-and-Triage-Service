# ClaimFlow runbook

## Start and inspect

From the project directory:

```powershell
docker compose up --build -d
docker compose ps
docker compose logs -f api intake-worker coverage-worker corpus-indexer
```

The web workspace is at `http://localhost:8080`; API documentation is at `http://localhost:8000/docs`; Grafana is at `http://localhost:3000`; Prometheus is at `http://localhost:9090`; Jaeger is at `http://localhost:16686`. The optional Locust page is at `http://localhost:8089` after starting its profile. Check `/health/ready` before sending traffic. The indexer must complete successfully before the API and workers become ready.

## FNOL lifecycle

1. A client posts a validated FNOL to `POST /api/v1/runs`. The response includes a run ID and correlation ID; poll `GET /api/v1/runs/{run_id}`.
2. Invalid input is stored in the quarantine queue and returned as 422. Confirm `agent_run_started: false`; review `GET /api/v1/quarantine` and correct the submitted fields before resubmission.
3. Intake screens untrusted text, extracts fields, and classifies the report. A safe handoff is queued for coverage review.
4. Coverage searches the policy corpus, retrieves restrictions and exceptions, and verifies citation IDs and lexical support. Weak retrieval, unsupported scope, conflicting evidence, provider failure, or a guardrail hit routes the run to investigation. It never approves or rejects a claim.
5. Review the run timeline, clauses, verification note, route reason, token/cost data, and handler feedback in the workspace.

Statuses used by the API include `queued`, `intake_in_progress`, `coverage_pending`, `coverage_in_progress`, `completed`, `escalated`, and `quarantined`. `completed` means the automated review produced a verified result; its route may still be `investigation` or `surveyor_assignment` for a human handler. `escalated` means the automated review could not produce a verified result.

## Provider unavailable or slow

The LLM client uses bounded retries, timeout, and a Redis circuit breaker. If intake or coverage cannot complete, the workflow returns an investigation escalation without a confident coverage position. Check the `claimflow_llm_retries_total` metric and worker logs. For a safe demonstration, use **Simulate outage** in the UI or `POST /api/v1/demo/provider-outage` with `{"enabled":true,"duration_seconds":60}`. Disable it with `{"enabled":false}`. Confirm a new run reaches human review, then inspect the provider recovery and clear any open circuit via the endpoint.

If a run has a recorded coverage processing failure after intake, open its claim drawer and choose **Retry coverage analysis** after the service has recovered. The retry preserves the validated intake and existing audit timeline. A retry can still end in `escalated` if evidence cannot be verified; do not mark an unverified result as completed.

For an external OpenAI-compatible provider, set `LLM_BACKEND=openai_compatible`, `LLM_ENDPOINT`, `LLM_MODEL_ID`, and `LLM_API_KEY` in an untracked `.env`; configure input/output cost rates if known. Restart app services and submit a low-risk synthetic case. Never put credentials in prompts, logs, screenshots, or source control.

## Queue backlog or worker unavailable

- Inspect `intake-worker` and `coverage-worker` health and logs; view queue depth on the workspace or Prometheus.
- Restart only the affected worker with `docker compose restart intake-worker` or `docker compose restart coverage-worker`.
- RQ retries a failed job up to three times. Check the Redis-backed queue and the run status before manually resubmitting; use the same idempotency key to avoid duplicate runs.
- If intake queued successfully but coverage is delayed, retain the run ID and let the worker recover. A stuck or terminal failure must be reviewed by a handler; do not manufacture a verified result.
- A terminal coverage failure after intake can be retried from the claim drawer or with `POST /api/v1/runs/{run_id}/retry`; only runs with a recorded coverage failure are eligible.
- Scale coverage workers with `docker compose up -d --scale coverage-worker=3`. Each replica binds its own metrics port in its container; update Prometheus targets if per-replica metrics are needed.

## Corpus index stale or missing

Check `GET /api/v1/corpus/status` and `corpus-indexer` logs. The indexer downloads official policy PDFs when available; otherwise it indexes the compact, labeled offline excerpts in `corpus/`. `source_fetch_status` distinguishes these modes. To rebuild after a source/prompt-independent wording change, run `docker compose run --rm -e FORCE_REEMBED=1 corpus-indexer`, then restart API and workers. Rebuilding replaces the shared index. Confirm the manifest fingerprint, chunk count, and source versions before handling cases.

## Database, Redis, and data recovery

PostgreSQL stores FNOLs, run events, quarantine entries, feedback, and quality samples; Redis stores asynchronous queues, rate-limit counters, circuit state, and short-lived retrieval cache. Docker volumes `postgres-data` and `redis-data` persist across container recreation. Back up before removing volumes. For a logical PostgreSQL backup use `docker compose exec -T postgres pg_dump -U postgres claims > claims-backup.sql`; restore into a deliberately prepared database with `psql`. Do not use `docker compose down -v` unless you intend to erase all local project data.

## SLO and observability

The current target is end-to-end p95 at or below 15 seconds (configurable with `RUN_SLO_P95_SECONDS`). Compare workspace latency with Prometheus `histogram_quantile(0.95, sum by (le) (rate(claimflow_run_latency_seconds_bucket[5m])))`. Use Grafana for trends, Jaeger for trace spans, and logs for correlation IDs. The target is a development objective, not a measured guarantee. Record load, worker count, provider, corpus fingerprint, and result files before drawing capacity conclusions.

## Data handling and safe operation

The project is a local demonstration, has no user authentication or authorization, and is not ready to receive real claim or personal data. Keep its ports on a trusted machine, use synthetic FNOL data, and use a production identity, secrets, retention, audit, security, and review design before deployment. Do not treat generated positions as coverage decisions or payment estimates.
