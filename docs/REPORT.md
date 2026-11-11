# Motor Insurance Claim Intake and Triage Service

## Abstract

ClaimFlow is an asynchronous, human-reviewed motor claim intake and triage demonstration. It accepts a First Notice of Loss (FNOL), validates and quarantines malformed input, extracts incident fields with an intake agent, retrieves relevant policy passages with a separate coverage agent, verifies citations, and routes the result for fast-track handling, surveyor assignment, or investigation. The service records prompt versions, model usage, latency, retries, quality samples, feedback, queue health, metrics, and traces. It makes advisory recommendations only; a claims handler remains responsible for decisions.

## Problem and objectives

Claim intake combines unstructured incident descriptions, structured policy details, procedural evidence, and restrictions that can change the apparent meaning of an initial coverage clause. The project demonstrates an auditable workflow that separates extraction from policy analysis, retrieves both a possible grant and modifying provisions, rejects malformed payloads before model execution, requires resolvable citations, and escalates unsupported results.

## Scope and corpus

The index contains the New India Assurance Private Car Base Package Policy wording, the insurer-hosted India Motor Tariff 2002 private-car use section, and the insurer's Motor Insurance Do's and Don'ts. Runtime indexing downloads the source PDFs when reachable and records hashes, index version, chunk count, source status, and build duration. The offline excerpt corpus is used when remote retrieval is unavailable. The indexed scope is narrow: it is not a substitute for an issued policy schedule, endorsements, current legal advice, adjuster evidence, or insurer claim handling rules.

The representative difficult case is a vehicle involved in a food delivery. The policy's accidental damage grant is considered alongside a separate permitted-use limit concerning carriage of goods/trade. The relationship is routed for human review instead of making a binary determination. This is the intended behavior of the demonstration, not a legal conclusion for a real claim.

## Architecture and MLOps lifecycle

The Docker Compose stack contains PostgreSQL, Redis, an OpenAI-compatible local stub, a one-shot corpus indexer, a FastAPI service, independent intake and coverage workers, an Nginx workspace, Prometheus, Grafana, Jaeger, cAdvisor, and an optional Locust load generator. The two LangGraph workflows communicate through Redis/RQ queues; PostgreSQL persists claim records and auditable events. Local word/bigram hashing and FAISS provide repeatable retrieval without a hosted embedding key. Prompt files are versioned separately. A runtime provider can be configured through environment variables.

Submission validation enforces dates, Indian registration format, enumerated loss and vehicle-use types, and insurer/product scope. The API supports idempotency, correlation IDs, request rate limiting, quarantine records, and asynchronous polling. Intake free text is treated as untrusted and screened for instruction injection and toxicity before model use. Coverage analysis has bounded steps, tool calls, wall time, and tool timeouts. Citation IDs must resolve; a lexical support check guards against unsupported summaries. Low confidence, missing evidence, guardrails, or model outages lead to human investigation.

## Evaluation and monitoring

`evaluation/cases.json` defines 20 labeled scenarios and `evaluation/evaluate.py` runs them through the live asynchronous service. It scores position, route, escalation, classification, guardrail, and citation-integrity agreement, and includes a malformed-input quarantine check. `app/loadtest/locustfile.py` submits and polls synthetic requests. The repository includes Grafana provisioning, Prometheus service/agent/cAdvisor scrapes, trace export to Jaeger, health endpoints, dashboard measures for queue depth and p50/p95 latency, token/cost estimates, worker uptime, retries, feedback, and sampled quality indicators.

No benchmark or load result is reported here because the stack has not yet been run in this workspace. The scripts save actual measurements to `evaluation/results/`; report those artifacts along with hardware, worker count, provider/model, rate limit, corpus fingerprint, and test duration. Heuristic live quality samples and lexical overlap are monitoring signals, not a substitute for human-labelled factuality review.

## Limitations and next steps

This is a local educational prototype with no authentication, authorization, real document OCR, policy schedule ingestion, claims database integration, secure PII controls, or operational human queue. The compact deterministic stub is for end-to-end repeatability, not model quality. Retrieval uses sparse local hashing, and evaluation ground truth is small and hand-authored. A production system would require an insurer-approved corpus and legal review, access controls, privacy and retention controls, threat modelling, independent evaluation by claim experts, resilient deployment, and a governed human review interface.
