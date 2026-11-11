from prometheus_client import Counter, Gauge, Histogram, start_http_server

http_requests = Counter(
    "claimflow_http_requests_total", "HTTP requests handled", ["method", "path", "status"]
)
submitted_runs = Counter("claimflow_runs_submitted_total", "Valid FNOL runs submitted")
completed_runs = Counter(
    "claimflow_runs_completed_total", "Completed and escalated runs", ["status"]
)
quarantined_requests = Counter(
    "claimflow_quarantined_requests_total", "Malformed requests quarantined"
)
agent_tasks = Counter(
    "claimflow_agent_tasks_total", "Agent tasks handled", ["agent", "result"]
)
agent_steps = Counter("claimflow_agent_steps_total", "Agent reasoning steps", ["agent"])
tool_calls = Counter("claimflow_tool_calls_total", "Agent tool calls", ["tool"])
step_latency = Histogram(
    "claimflow_step_latency_seconds",
    "Agent step latency",
    ["agent", "step"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)
run_latency = Histogram(
    "claimflow_run_latency_seconds",
    "Submission-to-final-run latency",
    buckets=(1, 2, 5, 10, 15, 20, 30, 45, 60, 90),
)
llm_retries = Counter("claimflow_llm_retries_total", "Model retries", ["agent"])
retrieval_cache_hits = Counter("claimflow_retrieval_cache_hits_total", "Cached retrieval responses")
feedback_records = Counter("claimflow_feedback_total", "Feedback submitted", ["satisfactory"])
queue_depth = Gauge("claimflow_queue_depth", "Pending RQ queue depth", ["queue"])
container_uptime = Gauge("claimflow_container_uptime_seconds", "Worker process uptime", ["agent"])


def start_worker_metrics(port: int) -> None:
    start_http_server(port)
