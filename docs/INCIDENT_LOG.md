# Demonstration incident log

This is a runbook worksheet. No operational incident or load result has been recorded for this local project yet. Add real timestamps and evidence after a demonstration; do not backfill simulated events as production incidents.

| ID | Started / ended (UTC) | Impact | Detection | Action taken | Recovery evidence | Follow-up |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — |

## Suggested tabletop exercises

- **Provider outage:** activate the bounded outage demonstration; verify retries, circuit behavior, correlation logging, and human-review fallback.
- **Coverage queue backlog:** pause a worker in a disposable environment, observe queue depth, restart it, and confirm idempotent completion.
- **Unavailable source PDF:** set `CORPUS_FETCH_REMOTE=false`, rebuild in a disposable index, and confirm the offline excerpt source is shown in the corpus manifest and citations.
- **Malformed FNOL:** send an invalid synthetic payload, confirm a quarantine record exists and no agent was started, then document the returned correlation ID.

Record the affected version, stack configuration, corpus fingerprint, run IDs, observed metrics, and cleanup steps for each exercise.
