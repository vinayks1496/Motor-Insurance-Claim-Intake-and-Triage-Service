(() => {
  const content = document.querySelector(".content");
  const overviewMarkup = content.innerHTML;
  const navItems = [...document.querySelectorAll("[data-view]")];
  const detailPanel = document.getElementById("detail-panel");
  const detailContent = document.getElementById("detail-content");
  const detailBackdrop = document.getElementById("detail-backdrop");
  const toast = document.getElementById("toast");
  let activeView = "overview";
  let toastTimer;
  let detailPollTimer;
  let refreshBusy = false;

  const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : escapeHTML(value);
  const dateLabel = (value) => value ? new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—";
  const shortId = (value) => value ? `${value.slice(0, 8)}…` : "—";
  const formatSeconds = (value) => Number(value || 0).toFixed(1);

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.detail?.error || body.detail || body.error || `Request failed (${response.status})`);
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return body;
  }

  function notify(message, isError = false) {
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 3400);
  }

  function setActive(view) {
    activeView = view;
    navItems.forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    const labels = { overview: "Overview", claims: "Claims", evidence: "Policy evidence", feedback: "Feedback", evaluation: "Evaluation" };
    const crumb = document.getElementById("page-crumb");
    if (crumb) crumb.textContent = labels[view] || "Overview";
  }

  function pageHeading(eyebrow, title, description, action = "") {
    return `<div class="page-heading"><div><p class="eyebrow">${escapeHTML(eyebrow)}</p><h1>${escapeHTML(title)}</h1><p class="subheading">${escapeHTML(description)}</p></div>${action}</div>`;
  }

  function statusChip(value) {
    const label = String(value || "queued").replaceAll("_", " ");
    return `<span class="status-pill status-${escapeHTML(value || "queued")}">${escapeHTML(label)}</span>`;
  }

  function runRow(run) {
    const fnol = run.fnol || {};
    const result = run.result || {};
    return `<tr class="run-row" data-run-id="${escapeHTML(run.run_id)}" tabindex="0" role="button" aria-label="Open claim ${escapeHTML(fnol.client_reference || shortId(run.run_id))}">
      <td><strong>${text(fnol.client_reference, shortId(run.run_id))}</strong><small>${text(fnol.registration_number)} · ${text(fnol.policy_number)}</small></td>
      <td>${text(fnol.loss_kind, "other").replaceAll("_", " ")}<small>${text(fnol.vehicle_use, "unknown")} use</small></td>
      <td>${text(result.route, "pending").replaceAll("_", " ")}</td><td>${statusChip(run.status)}</td><td>${dateLabel(run.created_at)}</td><td class="row-arrow">↗</td></tr>`;
  }

  function bindRunRows(container) {
    container.querySelectorAll("[data-run-id]").forEach((row) => {
      const open = () => openRun(row.dataset.runId);
      row.addEventListener("click", open);
      row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
    });
  }

  function drawQuality(trend = []) {
    const target = document.getElementById("quality-bars");
    if (!target) return;
    const count = document.getElementById("quality-count");
    if (count) count.textContent = `${trend.length} sample${trend.length === 1 ? "" : "s"}`;
    if (!trend.length) {
      target.innerHTML = '<div class="quality-empty">Quality trend appears as live runs are sampled.</div>';
      return;
    }
    const last = trend.at(-1);
    const metrics = [["groundedness", last.groundedness], ["faithfulness", last.faithfulness], ["context relevance", last.context_relevance]];
    target.innerHTML = metrics.map(([label, value]) => `<div class="quality-metric"><span>${escapeHTML(label)}</span><div class="quality-track"><i style="width:${Math.round(Math.max(0, Math.min(1, Number(value || 0))) * 100)}%"></i></div><b>${Math.round(Number(value || 0) * 100)}%</b></div>`).join("");
  }

  async function refreshOverview() {
    if (refreshBusy || activeView !== "overview") return;
    refreshBusy = true;
    try {
      const [dashboard, corpus] = await Promise.all([api("/api/v1/dashboard"), api("/api/v1/corpus/status")]);
      const totals = dashboard.totals || {};
      const performance = dashboard.performance || {};
      const set = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
      set("stat-runs", totals.runs_in_sample ?? 0);
      set("stat-queued", totals.queued ?? 0);
      set("stat-escalated", totals.escalated ?? 0);
      set("stat-p95", formatSeconds(performance.p95_seconds));
      const slo = document.getElementById("slo-state");
      if (slo) { slo.textContent = performance.slo_met ? "within 15 sec target" : `target ${dashboard.slo?.p95_seconds ?? 15} sec`; slo.classList.toggle("met", !!performance.slo_met); }
      const routes = totals.routes || {};
      set("route-fast", routes["fast-track"] || 0);
      set("route-surveyor", routes.surveyor_assignment || 0);
      set("route-investigation", routes.investigation || 0);
      drawQuality(dashboard.quality?.trend || []);
      const corpusMeta = document.getElementById("corpus-meta");
      if (corpusMeta) corpusMeta.textContent = `${corpus.chunks || 0} passages · ${corpus.embedding_strategy || "local index"}`;
      const body = document.getElementById("runs-body");
      if (body) {
        const runs = dashboard.recent_runs || [];
        body.innerHTML = runs.length ? runs.map(runRow).join("") : '<tr><td colspan="6" class="empty-row">No claims submitted yet. Start with a synthetic FNOL above.</td></tr>';
        bindRunRows(body);
        set("run-table-summary", `Showing ${runs.length} recent claim${runs.length === 1 ? "" : "s"}`);
      }
    } catch (error) {
      notify(`Workspace data is unavailable: ${error.message}`, true);
    } finally { refreshBusy = false; }
  }

  async function refreshNavCount() {
    const badge = document.getElementById("nav-claims-count");
    if (!badge) return;
    try {
      const result = await api("/api/v1/runs/count");
      badge.textContent = Number(result.total || 0).toLocaleString();
    } catch {
      // Keep the last known count visible while the API is temporarily unavailable.
    }
  }

  function showOverview() {
    content.innerHTML = overviewMarkup;
    setActive("overview");
    bindOverviewForm();
    refreshOverview();
  }

  async function renderClaims() {
    content.innerHTML = `${pageHeading("workspace / activity", "Claims", "Search and inspect the full run history.", '<button class="button button-quiet button-small" id="claims-refresh">Refresh ↻</button>')}
      <section class="panel runs-panel view-panel"><div class="panel-heading"><div><p class="eyebrow">all submissions</p><h2>Claim runs</h2></div><label class="searchbox"><span>⌕</span><input id="claims-search" type="search" placeholder="Search reference, vehicle or status" /></label></div><div class="table-wrap"><table><thead><tr><th>Claim / vehicle</th><th>Incident</th><th>Queue</th><th>Status</th><th>Submitted</th><th></th></tr></thead><tbody id="claims-body"><tr><td class="empty-row" colspan="6">Loading claims…</td></tr></tbody></table></div></section>`;
    const body = document.getElementById("claims-body");
    try {
      const data = await api("/api/v1/runs?limit=100");
      const runs = data.runs || [];
      body.innerHTML = runs.length ? runs.map(runRow).join("") : '<tr><td class="empty-row" colspan="6">No claims submitted.</td></tr>';
      bindRunRows(body);
      document.getElementById("claims-search").addEventListener("input", (event) => {
        const query = event.target.value.toLowerCase();
        body.querySelectorAll("tr[data-run-id]").forEach((row) => row.classList.toggle("hidden", !row.textContent.toLowerCase().includes(query)));
      });
    } catch (error) { body.innerHTML = `<tr><td colspan="6" class="empty-row">${escapeHTML(error.message)}</td></tr>`; }
    document.getElementById("claims-refresh").addEventListener("click", renderClaims);
  }

  async function renderEvidence() {
    content.innerHTML = `${pageHeading("workspace / corpus", "Policy evidence", "The retrieval index keeps the source document, clause, page, and corpus build information attached.")}
      <section class="panel view-panel"><div class="panel-heading"><div><p class="eyebrow">retrieval index</p><h2>Corpus status</h2></div><span class="status-chip"><span class="pulse-dot"></span>live</span></div><div id="corpus-summary" class="detail-meta"><div><span>Index state</span><strong>Loading</strong></div></div></section>
      <section class="panel view-panel"><div class="panel-heading"><div><p class="eyebrow">source documents</p><h2>Indexed references</h2></div></div><div id="source-list" class="source-list"><p class="quality-empty">Loading source manifest…</p></div></section>`;
    try {
      const [manifest, sourceManifest] = await Promise.all([api("/api/v1/corpus/status"), api("/api/v1/corpus/sources")]);
      const summary = document.getElementById("corpus-summary");
      summary.innerHTML = `<div><span>Passages</span><strong>${text(manifest.chunks, "0")}</strong></div><div><span>Index built</span><strong>${dateLabel(manifest.built_at)}</strong></div><div><span>Build time</span><strong>${text(manifest.build_seconds, "—")} sec</strong></div><div><span>Embedding</span><strong>${text(manifest.embedding_strategy)}</strong></div><div><span>Chunk strategy</span><strong>${text(manifest.chunking_strategy)}</strong></div><div><span>Fingerprint</span><strong>${text(manifest.fingerprint?.slice(0, 18), "—")}…</strong></div>`;
      document.getElementById("source-list").innerHTML = (sourceManifest.sources || []).map((source) => {
        const fetch = manifest.source_fetch_status?.[source.document_id] || {};
        const url = /^https:\/\//i.test(source.url || "") ? source.url : "#";
        return `<article class="source-card"><div><p class="eyebrow">${text(source.type, "policy document")}</p><h3>${text(source.title)}</h3><p>${text(source.version)}</p><small>${fetch.status === "ready" ? "Official PDF indexed" : "Offline excerpt available · remote PDF " + text(fetch.status, "not downloaded")}</small></div><a class="button button-quiet button-small" href="${escapeHTML(url)}" target="_blank" rel="noreferrer">Open source ↗</a></article>`;
      }).join("");
    } catch (error) { document.getElementById("source-list").innerHTML = `<p class="quality-empty">Corpus details could not load: ${escapeHTML(error.message)}</p>`; }
  }

  async function renderFeedback() {
    content.innerHTML = `${pageHeading("workspace / handler loop", "Feedback", "Claims handlers can rate a completed review and leave a reason for future prompt and retrieval improvements.")}
      <section class="stats-grid feedback-stats"><article class="stat-card"><div class="stat-top"><span class="eyebrow">responses</span></div><div class="stat-value" id="feedback-total">—</div><div class="stat-foot">Recorded handler reviews</div></article><article class="stat-card"><div class="stat-top"><span class="eyebrow">satisfactory</span></div><div class="stat-value" id="feedback-positive">—</div><div class="stat-foot">Marked helpful</div></article><article class="stat-card"><div class="stat-top"><span class="eyebrow">needs work</span></div><div class="stat-value" id="feedback-negative">—</div><div class="stat-foot">Marked for improvement</div></article></section>
      <section class="panel view-panel"><div class="panel-heading"><div><p class="eyebrow">review loop</p><h2>Recent handler feedback</h2></div><button class="button button-quiet button-small" data-view="claims">Review claims ↗</button></div><div class="feedback-list" id="feedback-list"><p class="quality-empty">Loading feedback…</p></div></section>`;
    try {
      const [summary, recent] = await Promise.all([api("/api/v1/feedback/summary"), api("/api/v1/feedback/recent?limit=50")]);
      document.getElementById("feedback-total").textContent = summary.total;
      document.getElementById("feedback-positive").textContent = summary.satisfactory;
      document.getElementById("feedback-negative").textContent = summary.unsatisfactory;
      const list = document.getElementById("feedback-list");
      list.innerHTML = recent.records.length ? recent.records.map((row) => `<article class="feedback-record"><div><strong>${row.satisfactory ? "Helpful" : "Needs improvement"}</strong><small>${dateLabel(row.created_at)} · run ${shortId(row.run_id)}</small><p>${text(row.reason)}</p></div><button class="button button-quiet button-small" data-run-id="${escapeHTML(row.run_id)}">Open claim ↗</button></article>`).join("") : '<p class="quality-empty">No feedback yet. Open a completed claim to leave the first handler rating.</p>';
      bindRunRows(list);
    } catch (error) { document.getElementById("feedback-list").innerHTML = `<p class="quality-empty">Feedback could not load: ${escapeHTML(error.message)}</p>`; }
  }

  async function renderEvaluation() {
    content.innerHTML = `${pageHeading("workspace / quality", "Evaluation", "A labeled 20-case regression suite tracks routing, extraction, escalation, and citation integrity.", '<button class="button button-quiet button-small" id="eval-refresh">Refresh ↻</button>')}
      <section class="stats-grid evaluation-stats" id="eval-stats"><article class="stat-card"><div class="stat-top"><span class="eyebrow">suite</span></div><div class="stat-value">20</div><div class="stat-foot">labeled cases</div></article><article class="stat-card"><div class="stat-top"><span class="eyebrow">last run</span></div><div class="stat-value" id="eval-state">—</div><div class="stat-foot" id="eval-when">Loading evaluation state</div></article></section>
      <section class="panel view-panel"><div class="panel-heading"><div><p class="eyebrow">case coverage</p><h2>Expected triage labels</h2></div><span class="panel-index">20 CASES</span></div><div class="table-wrap"><table><thead><tr><th>Scenario</th><th>Expected position</th><th>Route</th><th>Human escalation</th><th>Latest result</th></tr></thead><tbody id="eval-body"><tr><td colspan="5" class="empty-row">Loading evaluation cases…</td></tr></tbody></table></div><div class="eval-command"><p>Run the full suite from the project folder after starting the stack:</p><code>python evaluation/evaluate.py --base-url http://localhost:8000</code></div></section>`;
    document.getElementById("eval-refresh").addEventListener("click", renderEvaluation);
    try {
      const report = await api("/api/v1/evaluation/summary");
      document.getElementById("eval-state").textContent = report.status === "complete" ? "Recorded" : "Not run";
      document.getElementById("eval-when").textContent = report.latest?.started_at ? `Latest suite ${report.latest.started_at}` : "Run evaluation to create measured results";
      const scores = report.latest?.summary || {};
      const stats = document.getElementById("eval-stats");
      if (report.latest) {
        stats.insertAdjacentHTML("beforeend", ["position_match", "route_match", "escalation_match", "citation_integrity"].map((key) => `<article class="stat-card"><div class="stat-top"><span class="eyebrow">${escapeHTML(key.replaceAll("_", " "))}</span></div><div class="stat-value">${Math.round((scores[key] || 0) * 100)}<small>%</small></div><div class="stat-foot">last evaluation</div></article>`).join(""));
      }
      const latestById = new Map((report.latest?.results || []).map((row) => [row.case_id, row]));
      const body = document.getElementById("eval-body");
      body.innerHTML = (report.case_definitions || []).map((item) => {
        const result = latestById.get(item.id);
        return `<tr><td><strong>${text(item.id)}</strong></td><td>${text(item.expected_position).replaceAll("_", " ")}</td><td>${text(item.expected_route).replaceAll("_", " ")}</td><td>${item.expected_escalated ? "Yes" : "No"}</td><td>${result ? (result.position_match && result.route_match ? '<span class="match-good">Match</span>' : '<span class="match-bad">Review</span>') : '<span class="muted-label">Not run</span>'}</td></tr>`;
      }).join("");
    } catch (error) { document.getElementById("eval-body").innerHTML = `<tr><td colspan="5" class="empty-row">Evaluation details could not load: ${escapeHTML(error.message)}</td></tr>`; }
  }

  async function setView(view) {
    closeDetail();
    setActive(view);
    refreshNavCount();
    if (view === "overview") return showOverview();
    if (view === "claims") return renderClaims();
    if (view === "evidence") return renderEvidence();
    if (view === "feedback") return renderFeedback();
    if (view === "evaluation") return renderEvaluation();
  }

  async function openRun(runId) {
    clearTimeout(detailPollTimer);
    detailPollTimer = null;
    detailPanel.classList.add("open");
    detailPanel.setAttribute("aria-hidden", "false");
    detailBackdrop.classList.remove("hidden");
    detailContent.innerHTML = '<div class="detail-loading">Loading claim, evidence and run timeline…</div>';
    try {
      const run = await api(`/api/v1/runs/${encodeURIComponent(runId)}`);
      const fnol = run.fnol || {};
      const result = run.result || {};
      const verification = result.verification || {};
      const citations = result.citations || [];
      const events = run.events || [];
      const guardrails = result.guardrail_flags || fnol._guardrails || {};
      const retryable = run.status === "escalated" && Boolean(run.intake) && events.some((event) => event.agent === "coverage" && event.event_type === "agent_failure");
      detailContent.innerHTML = `${retryable ? '<section class="detail-section retry-section"><button class="button button-quiet button-small" data-retry-coverage>Retry coverage analysis ↻</button><p class="muted-label">The intake is preserved. Retry after a temporary coverage-processing failure.</p></section>' : ""}<div class="detail-meta"><div><span>Run status</span><strong>${statusChip(run.status)}</strong>${run.status === "completed" && ["investigation", "surveyor_assignment"].includes(result.route) ? '<small>Verified review complete; handler follow-up remains.</small>' : ""}</div><div><span>Route</span><strong>${text(result.route, "pending").replaceAll("_", " ")}</strong></div><div><span>Position</span><strong>${text(result.coverage_position, "pending").replaceAll("_", " ")}</strong></div><div><span>Vehicle</span><strong>${text(fnol.registration_number)}</strong></div><div><span>Loss date</span><strong>${text(fnol.loss_date)}</strong></div><div><span>Estimate</span><strong>${fnol.estimate_inr == null ? "Not provided" : `₹${Number(fnol.estimate_inr).toLocaleString("en-IN")}`}</strong></div></div>
        <section class="recommendation-box"><p class="eyebrow">advisory summary</p><p>${text(result.summary, "This run is still being processed.")}</p>${result.route_reason ? `<p class="review-note">Handler note · ${text(result.route_reason)}</p>` : ""}${verification.note ? `<p class="review-note">Verification · ${text(verification.note)}</p>` : ""}${Object.values(guardrails).some(Boolean) ? '<span class="guardrail-chip">guardrail escalation</span>' : ""}</section>
        <div class="detail-meta"><div><span>Policy number</span><strong>${text(fnol.policy_number)}</strong></div><div><span>Incident type</span><strong>${text(fnol.loss_kind).replaceAll("_", " ")}</strong></div><div><span>Vehicle use</span><strong>${text(fnol.vehicle_use)}</strong></div><div><span>Correlation ID</span><strong>${text(run.correlation_id)}</strong></div></div>
        <section class="detail-section"><div class="detail-section-heading"><h3>Incident description</h3><span>${text(fnol.channel, "web")}</span></div><div class="citation-card"><p>${text(fnol.loss_description)}</p></div></section>
        <section class="detail-section"><div class="detail-section-heading"><h3>Policy evidence</h3><span>${citations.length} citations · ${verification.verified ? "verified" : "human review"}</span></div>${citations.length ? citations.map((cite) => { const link = /^https:\/\//i.test(cite.source_url || "") ? cite.source_url : "#"; return `<article class="citation-card"><div class="citation-top"><span class="citation-id">${text(cite.passage_id)}</span><span class="citation-score">${cite.relevance == null ? "source" : `${Math.round(cite.relevance * 100)}% match`}</span></div><p><strong>${text(cite.document_title)}</strong>${cite.page ? ` · page ${text(cite.page)}` : cite.section ? ` · ${text(cite.section)}` : ""}<br>${text(cite.text)}</p><a href="${escapeHTML(link)}" target="_blank" rel="noreferrer">Open source document ↗</a></article>`; }).join("") : '<p class="quality-empty">No verified citation was returned. This run must stay with a human handler.</p>'}</section>
        <section class="detail-section"><div class="detail-section-heading"><h3>Run timeline</h3><span>${events.length} events</span></div><div class="timeline">${events.map((event) => `<div class="timeline-item"><strong>${text(event.agent)} · ${text(event.event_type).replaceAll("_", " ")}</strong><small>${dateLabel(event.created_at)}${event.prompt_version ? ` · ${text(event.prompt_version)}` : ""}${event.payload?.to ? ` · to ${text(event.payload.to)}` : ""}${event.payload?.latency_ms ? ` · ${text(event.payload.latency_ms)} ms` : ""}</small></div>`).join("") || '<p class="quality-empty">Events will appear as agents work.</p>'}</div></section>
        <section class="detail-section"><div class="detail-section-heading"><h3>Usage and prompt</h3><span>${text(result.prompt_version, "—")}</span></div><div class="detail-meta"><div><span>Model calls</span><strong>${text(result.usage?.calls, "0")}</strong></div><div><span>Input / output tokens</span><strong>${text(result.usage?.input_tokens, "0")} / ${text(result.usage?.output_tokens, "0")}</strong></div><div><span>Estimated cost</span><strong>$${Number(result.usage?.cost_usd || 0).toFixed(6)}</strong></div><div><span>Created</span><strong>${dateLabel(run.created_at)}</strong></div></div></section>
        ${["completed", "escalated"].includes(run.status) ? `<section class="feedback-form"><p class="eyebrow">handler feedback</p><p>Was this advisory review useful? Add a short reason to support future evaluation.</p><textarea class="feedback-reason" id="feedback-reason" rows="2" minlength="4" maxlength="1000" placeholder="What was helpful or what should improve?"></textarea><div class="feedback-actions"><button class="button button-quiet button-small" data-feedback="true">Helpful</button><button class="button button-quiet button-small" data-feedback="false">Needs work</button></div></section>` : ""}`;
      const retryButton = detailContent.querySelector("[data-retry-coverage]");
      if (retryButton) retryButton.addEventListener("click", async () => {
        retryButton.disabled = true;
        try {
          await api(`/api/v1/runs/${encodeURIComponent(run.run_id)}/retry`, { method: "POST" });
          notify("Coverage retry queued. This panel will update automatically.");
          refreshNavCount();
          await openRun(run.run_id);
        } catch (error) {
          notify(`Coverage retry could not be queued: ${error.message}`, true);
          retryButton.disabled = false;
        }
      });
      const title = document.getElementById("detail-title");
      title.textContent = fnol.client_reference || `Run ${shortId(run.run_id)}`;
      detailContent.querySelectorAll("[data-feedback]").forEach((button) => button.addEventListener("click", async () => {
        const reason = document.getElementById("feedback-reason").value.trim();
        if (reason.length < 4) { notify("Add a short feedback reason first.", true); return; }
        try {
          await api(`/api/v1/runs/${encodeURIComponent(run.run_id)}/feedback`, { method: "POST", body: JSON.stringify({ satisfactory: button.dataset.feedback === "true", reason }) });
          notify("Handler feedback recorded.");
          if (activeView === "feedback") renderFeedback();
        } catch (error) { notify(`Feedback could not be saved: ${error.message}`, true); }
      }));
      if (!['completed', 'escalated', 'quarantined'].includes(run.status) && detailPanel.classList.contains("open")) {
        detailPollTimer = setTimeout(() => openRun(run.run_id), 2500);
      }
    } catch (error) { detailContent.innerHTML = `<div class="detail-loading">Claim details could not load: ${escapeHTML(error.message)}</div>`; }
  }

  function closeDetail() {
    clearTimeout(detailPollTimer);
    detailPollTimer = null;
    detailPanel.classList.remove("open");
    detailPanel.setAttribute("aria-hidden", "true");
    detailBackdrop.classList.add("hidden");
  }

  async function submitClaim(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = document.getElementById("submit-button");
    const alert = document.getElementById("form-alert");
    const values = Object.fromEntries(new FormData(form).entries());
    values.estimate_inr = values.estimate_inr ? Number(values.estimate_inr) : null;
    values.injury_reported = form.elements.injury_reported.checked;
    values.injury_count = values.injury_reported ? Number(values.injury_count || 1) : 0;
    values.police_reported = form.elements.police_reported.checked;
    button.disabled = true;
    button.textContent = "Submitting…";
    alert.classList.add("hidden");
    try {
      const accepted = await api("/api/v1/runs", { method: "POST", body: JSON.stringify(values), headers: { "Idempotency-Key": `web-${values.client_reference}` } });
      notify(`Claim queued · ${shortId(accepted.run_id)}`);
      refreshNavCount();
      form.reset();
      setDefaultDates();
      const clientRef = document.getElementById("client-reference");
      if (clientRef) clientRef.value = `CF-${Date.now().toString(36).toUpperCase()}`;
      await refreshOverview();
      openRun(accepted.run_id);
    } catch (error) {
      const details = error.body || {};
      if (error.status === 422) {
        const fields = (details.field_errors || []).map((item) => `${item.field}: ${item.message}`).join(" · ");
        alert.textContent = `${fields || error.message}${details.quarantine_id ? ` · quarantine #${details.quarantine_id}; no agent was started` : ""}`;
      } else alert.textContent = error.message;
      alert.classList.remove("hidden");
      notify("Claim was not submitted. Review the highlighted form response.", true);
    } finally { button.disabled = false; button.innerHTML = 'Submit for triage <span class="button-arrow">→</span>'; }
  }

  function setDefaultDates() {
    const today = new Date().toISOString().slice(0, 10);
    const loss = document.querySelector('[name="loss_date"]');
    const reported = document.querySelector('[name="reported_at"]');
    if (loss && !loss.value) loss.value = today;
    if (reported && !reported.value) reported.value = today;
  }

  function bindOverviewForm() {
    const form = document.getElementById("claim-form");
    if (!form) return;
    setDefaultDates();
    const ref = document.getElementById("client-reference");
    if (ref && !ref.value) ref.value = `CF-${Date.now().toString(36).toUpperCase()}`;
    form.addEventListener("submit", submitClaim);
    document.getElementById("new-claim-button").addEventListener("click", () => document.getElementById("intake-panel").scrollIntoView({ behavior: "smooth" }));
    document.getElementById("refresh-button").addEventListener("click", refreshOverview);
    const injury = form.elements.injury_reported;
    injury.addEventListener("change", () => document.getElementById("injury-count-wrap").classList.toggle("hidden", !injury.checked));
    document.getElementById("food-delivery-example").addEventListener("click", () => {
      form.elements.loss_description.value = "While completing a paid food delivery, the car hit a divider and sustained front bumper damage during the trip.";
      form.elements.vehicle_use.value = "delivery";
      form.elements.loss_kind.value = "collision";
      form.elements.estimate_inr.value = "54000";
      document.getElementById("intake-panel").scrollIntoView({ behavior: "smooth" });
    });
    document.getElementById("provider-demo").addEventListener("click", async () => {
      try {
        await api("/api/v1/demo/provider-outage", { method: "POST", body: JSON.stringify({ enabled: true, duration_seconds: 60 }) });
        notify("Provider outage simulation enabled for 60 seconds.");
      } catch (error) { notify(`Outage simulation failed: ${error.message}`, true); }
    });
    const search = document.getElementById("run-search");
    search.addEventListener("input", () => document.querySelectorAll("#runs-body tr[data-run-id]").forEach((row) => row.classList.toggle("hidden", !row.textContent.toLowerCase().includes(search.value.toLowerCase()))));
  }

  document.addEventListener("click", (event) => {
    const item = event.target.closest("[data-view]");
    if (!item) return;
    event.preventDefault();
    if (item.dataset.view) setView(item.dataset.view);
  });
  document.getElementById("close-detail").addEventListener("click", closeDetail);
  detailBackdrop.addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDetail(); });
  showOverview();
  refreshNavCount();
  setInterval(refreshNavCount, 5000);
  setInterval(refreshOverview, 5000);
})();
