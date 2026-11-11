# Evaluation suite

`cases.json` contains 20 labeled, deterministic FNOL scenarios covering ordinary damage, theft, injuries, possible total loss, high estimates, delivery and commercial use restrictions, unsupported policy scope, and prompt injection. Labels are advisory position and routing expectations; they are not claim decisions.

Run from the repository root after starting the stack:

```powershell
python -m pip install httpx
python evaluation/evaluate.py --base-url http://localhost:8000
```

The harness submits each case, polls the asynchronous workflow, checks expected classification, position, route, escalation, citations, and guardrail behavior, then submits one malformed request to check quarantine. It waits 8.2 seconds between submissions to stay below the default per-client limit. Results are written to `evaluation/results/` as JSON and CSV; `latest.json` is consumed by the dashboard. With the local stub, 20 sequential runs take several minutes.

The ground truth is an intentionally small regression set. Review the saved per-case rows and citations before changing prompts or corpus versions. A match score measures agreement with this suite only; it does not establish policy correctness or production readiness. A live LLM provider may produce a different route or safely escalate, so retain the raw outputs and review disagreements.

## Load profile

See `app/loadtest/locustfile.py` for users that submit and poll full claim workflows. The Locust profile lives in `compose.yaml`; use the repeatable commands in the root README. Store the Locust CSV output in `evaluation/results/` and record the workload and environment beside it. Increase the API rate limit only in a local benchmark `.env` when measuring throughput above the default protection limit.

## Result status

No evaluation metrics are checked in as if they were measured. Run the harness against the deployed stack to create the first result report.
