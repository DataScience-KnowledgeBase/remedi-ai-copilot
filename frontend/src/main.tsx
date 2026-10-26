import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Incident = {
  id: string; title: string; service: string; severity: string; status: string;
  scenario: string; created_at: string; provider: string; model: string;
  error: string | null;
  plan: { action: string; confidence: number; summary: string; evidence_ids: string[] } | null;
};
type AuditEvent = {
  seq: number; at: string; stage: string; actor: string; message: string;
  payload: Record<string, unknown>;
};
type Configuration = {
  role: string; provider: string; model: string; confidence_threshold: number;
  max_agent_steps: number; prompt_version: string; runbook_version: string;
  pricing: { input_usd_per_million: number; output_usd_per_million: number; gpu_usd_per_hour: number };
  billing_basis: string;
  kubernetes?: { enabled: boolean; namespace: string; owner_approval_required: boolean };
  github_gitops?: { enabled: boolean; repository: string; approval_action: string; manual_merge_required: boolean };
};
type Telemetry = {
  incident_count: number; llm_calls: number; input_tokens: number; output_tokens: number;
  estimated_usd: number | null; p95_llm_ms: number | null; outcomes: Record<string, number>;
  calls_with_missing_usage: number;
};
type Runbook = { id: string; action: string; risk: string; description: string; rollback: string };
type Feedback = { incident_id: string; outcome: string; candidate: string };
const scenarios = [
  ["dynamic_cache", "Dynamic schema mismatch", "LLM diagnosis + independent review / low risk"],
  ["dynamic_worker", "Dynamic worker deadlock", "LLM diagnosis + review / owner approval"],
  ["dynamic_capacity", "Dynamic demand saturation", "LLM capacity recommendation / owner approval"],
  ["cache_pressure", "Cache pressure", "Low risk / automatic recovery"],
  ["worker_stall", "Worker stalled", "High impact / owner approval"],
  ["unknown_signal", "Unknown signal", "Uncertain / escalate"],
  ["verification_failure", "Recovery failure", "Verify / rollback"],
  ["prompt_injection", "Untrusted signal", "Injection / policy boundary"],
  ["k8s_crashloop", "Kubernetes CrashLoopBackOff", "REAL pods / AI diagnosis / owner approval"],
  ["k8s_oom", "Kubernetes OOMKilled", "REAL pods / AI diagnosis / owner approval"],
  ["k8s_imagepull", "Kubernetes ImagePullBackOff", "REAL pods / AI diagnosis / owner approval"],
  ["k8s_pending", "Kubernetes Pending / FailedScheduling", "REAL pods / AI diagnosis / owner approval"],
];
const terminal = new Set(["closed", "rolled_back", "escalated", "rejected", "failed"]);
const label = (value: string) => value.replaceAll("_", " ");

function App() {
  const [token, setToken] = useState("");
  const [draftToken, setDraftToken] = useState("");
  const [config, setConfig] = useState<Configuration | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selected, setSelected] = useState("");
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [runbooks, setRunbooks] = useState<Runbook[]>([]);
  const [feedback, setFeedback] = useState<Feedback[]>([]);
  const [tab, setTab] = useState("Operations");
  const [scenario, setScenario] = useState("cache_pressure");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [streamState, setStreamState] = useState("idle");
  const [streamRevision, setStreamRevision] = useState(0);
  const [filter, setFilter] = useState("");
  const [providerStatus, setProviderStatus] = useState("");
  const [repositoryStatus, setRepositoryStatus] = useState("");

  const api = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    const response = await fetch(`/api${path}`, {
      ...init, headers: { "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json", ...init.headers },
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(`${response.status}: ${detail}`);
    }
    return response.json() as Promise<T>;
  }, [token]);

  const refresh = useCallback(async () => {
    const [items, metrics] = await Promise.all([
      api<Incident[]>("/incidents"), api<Telemetry>("/telemetry"),
    ]);
    setIncidents(items); setTelemetry(metrics);
  }, [api]);

  const checkProvider = useCallback(async () => {
    const response = await fetch("/api/provider-status", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (response.status === 200 || response.status === 503) {
      const data = await response.json() as { ready: boolean; message: string };
      setProviderStatus(`${data.ready ? "READY" : "BLOCKED"}: ${data.message}`);
    } else {
      throw new Error(`Provider status check failed: ${response.status}`);
    }
  }, [token]);

  const checkRepository = useCallback(async () => {
    const response = await fetch("/api/repository-status", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (response.status !== 200 && response.status !== 503) {
      throw new Error(`Repository status check failed: ${response.status}`);
    }
    const data = await response.json() as { ready: boolean; message: string };
    setRepositoryStatus(`${data.ready ? "READY" : "BLOCKED"}: ${data.message}`);
  }, [token]);

  useEffect(() => {
    if (!token) return;
    let active = true;
    Promise.all([api<Configuration>("/config"), api<{ items: Runbook[] }>("/runbooks"),
      api<Feedback[]>("/feedback")]).then(([settings, books, outcomes]) => {
      if (active) { setConfig(settings); setRunbooks(books.items); setFeedback(outcomes); }
      if (active && settings.github_gitops?.enabled) {
        void checkRepository().catch((err: Error) => { if (active) setError(err.message); });
      }
    }).catch((err: Error) => { if (active) setError(err.message); });
    void checkProvider().catch((err: Error) => { if (active) setError(err.message); });
    const update = () => refresh().catch((err: Error) => { if (active) setError(err.message); });
    void update();
    const timer = window.setInterval(update, 6000);
    return () => { active = false; window.clearInterval(timer); };
  }, [token, api, refresh, checkProvider, checkRepository]);

  useEffect(() => {
    if (!selected || !token) return;
    const controller = new AbortController();
    setEvents([]); setStreamState("connecting");
    async function connect() {
      const response = await fetch(`/api/incidents/${selected}/stream`, {
        headers: { Authorization: `Bearer ${token}` }, signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`Event stream failed: ${response.status}`);
      setStreamState("live");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) { setStreamState("complete"); await refresh(); return; }
        pending += decoder.decode(value, { stream: true });
        let boundary: number;
        while ((boundary = pending.indexOf("\n\n")) !== -1) {
          const frame = pending.slice(0, boundary);
          pending = pending.slice(boundary + 2);
          const data = frame.split("\n").find(line => line.startsWith("data: "))?.slice(6);
          if (!data) continue;
          if (frame.includes("event: audit")) {
            const item = JSON.parse(data) as AuditEvent;
            setEvents(current => current.some(entry => entry.seq === item.seq) ? current : [...current, item]);
          } else if (frame.includes("event: status")) {
            const item = JSON.parse(data) as Incident;
            setIncidents(current => current.map(entry => entry.id === item.id ? item : entry));
          }
        }
      }
    }
    void connect().catch((err: Error) => {
      if (!controller.signal.aborted) { setError(err.message); setStreamState("disconnected"); }
    });
    return () => controller.abort();
  }, [selected, token, refresh, streamRevision]);

  async function login(event: React.FormEvent) {
    event.preventDefault(); setError(""); setConfig(null); setReason(""); setToken(draftToken);
    setDraftToken("");
  }
  async function simulate() {
    setBusy(true); setError("");
    try {
      const result = await api<{ incident: Incident }>("/incidents", {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ scenario }),
      });
      await refresh(); setSelected(result.incident.id);
    } catch (err) { setError(err instanceof Error ? err.message : "Incident creation failed"); }
    finally { setBusy(false); }
  }
  async function decide(decision: string) {
    setBusy(true); setError("");
    try {
      await api(`/incidents/${selected}/approval`, { method: "POST",
        body: JSON.stringify({ decision, reason }) });
      setReason(""); await refresh(); setStreamRevision(value => value + 1);
    } catch (err) { setError(err instanceof Error ? err.message : "Approval failed"); }
    finally { setBusy(false); }
  }
  function exportAudit() {
    const blob = new Blob([JSON.stringify({ incident: current, events }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a"); anchor.href = url;
    anchor.download = `remedi-audit-${selected}.json`; anchor.click(); URL.revokeObjectURL(url);
  }
  const current = incidents.find(item => item.id === selected);
  const realScenario = scenario.startsWith("k8s_");
  const repositoryMode = Boolean(config?.github_gitops?.enabled && current?.scenario.startsWith("k8s_"));
  const repositoryPlan = events.filter(event => event.stage === "repository_plan").at(-1);
  const pullRequest = events.find(event => event.stage === "github_pr");
  const pullRequestUrl = pullRequest?.payload.url;
  const filtered = incidents.filter(item => `${item.title} ${item.status} ${item.service}`.toLowerCase()
    .includes(filter.toLowerCase()));
  const priced = config?.provider === "ollama" ? config.pricing.gpu_usd_per_hour > 0
    : config?.provider === "api" && config.pricing.input_usd_per_million > 0
      && config.pricing.output_usd_per_million > 0;

  return <div className="shell">
    <aside>
      <div className="brand"><span className="logo">R</span>Remedi<span>AI</span></div>
      <div className="workspace">COPILOT API EDITION</div>
      <nav aria-label="Main navigation">{["Operations", "Runbooks", "Observability", "Learning"].map(item =>
        <button key={item} className={tab === item ? "nav active" : "nav"} onClick={() => setTab(item)}>
          <span>{({ Operations: "01", Runbooks: "02", Observability: "03", Learning: "04" })[item]}</span>{item}
        </button>)}</nav>
      <div className="boundary"><span className="dot" /> Governed by design
        <p>Automate only inside the approved risk boundary.</p>
        <small>{!config ? <>EXECUTION SCOPE NOT LOADED<br />Connect to inspect enabled adapters.</>
          : config.kubernetes?.enabled ? <>REAL KUBERNETES ENABLED<br />
          Namespace: {config.kubernetes.namespace}<br />Owner approval for every change.</>
          : <>SIMULATION ADAPTER<br />No production systems are modified.</>}</small>
      </div>
    </aside>
    <main>
      <header><div className="breadcrumb">Workspace / Incident intelligence</div>
        <div className="provider"><span className="dot" />{config ? `${config.provider} / ${config.model}` : "Not connected"}</div>
      </header>
      <section className="heading"><div><div className="eyebrow">GOVERNED INCIDENT REMEDIATION</div>
        <h1>{tab === "Operations" ? "Resolve with confidence." : tab}</h1>
        <p>Evidence-led decisions. Accountable actions. Verified recovery.</p></div>
        {config && <span className="badge">{config.role} session</span>}
      </section>
      <form className="auth panel" onSubmit={login}>
        <label htmlFor="token">Access token <small>(application OWNER_TOKEN to approve; not a GitHub token)</small></label>
        <input id="token" type="password" value={draftToken} onChange={e => setDraftToken(e.target.value)}
          placeholder="Enter operator or owner token" required minLength={32} autoComplete="off" />
        <button type="submit">Connect</button>
        {token && <button type="button" className="secondary" onClick={() => {
          setToken(""); setConfig(null); setIncidents([]); setEvents([]); setSelected("");
          setTelemetry(null); setRunbooks([]); setFeedback([]);
          setProviderStatus("");
          setRepositoryStatus("");
        }}>Disconnect</button>}
      </form>
      {error && <div className="error" role="alert">{error}<button onClick={() => setError("")}>Dismiss</button></div>}
      {!config && <div className="empty panel">Connect to begin. The console never stores credentials in browser storage.</div>}
      {config && <>
        <section className="panel provider-connection">
          <div className="section-title"><h2>LLM connection</h2><span className="badge">{config.billing_basis}</span></div>
          <p role="status">{providerStatus || "Checking supported provider authentication..."}</p>
          <button className="secondary" onClick={() => void checkProvider().catch((err: Error) => setError(err.message))}>Recheck provider</button>
          {config.provider === "copilot" && <p>Official GitHub Copilot SDK / built-in tools disabled / no GPU required.
            Sign in locally with scripts\copilot-login.ps1. Subscription limits and organization policies apply.</p>}
        </section>
        {config.github_gitops?.enabled && <section className="panel provider-connection">
          <div className="section-title"><h2>Repository-backed deployment</h2><span className="badge">Human merge required</span></div>
          <p>{config.github_gitops.repository} / GitHub App credentials stay on the server.</p>
          <p role="status">{repositoryStatus || "Checking repository configuration..."}</p>
          <button className="secondary" onClick={() => void checkRepository().catch((err: Error) => setError(err.message))}>Recheck repository</button>
          <p>Owner approval creates the reviewed PR only. Manual merge and successful CI gate deployment.
            The application never merges PRs or bypasses protection.</p>
        </section>}
        <section className="stats" aria-label="Operational metrics">
          <Metric label="INCIDENTS" value={String(telemetry?.incident_count ?? 0)} note="Last 100 incidents" />
          <Metric label="VERIFIED RECOVERIES" value={String(telemetry?.outcomes.closed ?? 0)} note="Three health probes passed" />
          <Metric label="P95 INFERENCE" value={telemetry?.p95_llm_ms != null ? `${(telemetry.p95_llm_ms / 1000).toFixed(2)}s` : "--"} note="Per-call latency, includes retries" />
          <Metric label="BILLING / COST" value={config.provider === "copilot" ? "Subscription" : config.provider === "demo" ? "Fixture" : priced ? `$${(telemetry?.estimated_usd ?? 0).toFixed(5)}` : "Unpriced"} note={config.provider === "copilot" ? "Account entitlement / not unlimited or USD" : config.provider === "demo" ? "No LLM inference" : priced ? "Configured estimate / not a bill" : "Configure GPU or token rates"} />
        </section>
        {tab === "Operations" && <>
          <section className="panel flow"><div className="section-title"><h2>Recovery control plane</h2><span className="badge">Google ADK + LLM review</span></div>
            <div className="steps">{["Incident", "AI triage", "Policy decision", "Execute", "Verify", "Close + learn"].map((step, index) =>
              <div key={step}><b>{String(index + 1).padStart(2, "0")}</b><span>{step}</span></div>)}</div>
            <p>High impact or low confidence <span className="arrow">&rarr;</span> Recommend <span className="arrow">&rarr;</span> Owner approval <span className="arrow">&rarr;</span> Deploy + verify / rollback</p>
          </section>
          <section className="simulate panel"><div><h2>{realScenario ? "Diagnose real Kubernetes pods" : "Inject a synthetic incident"}</h2>
            <p>{realScenario ? `Read live events in ${config.kubernetes?.namespace}. No cluster changes until owner approval.`
              : "Exercise a complete, auditable workflow without production access."}</p></div>
            <label className="sr-only" htmlFor="scenario">Incident scenario</label>
            <select id="scenario" value={scenario} onChange={e => setScenario(e.target.value)}>{scenarios.filter(([id]) =>
              !id.startsWith("k8s_") || config.kubernetes?.enabled).map(([id, name, hint]) =>
              <option key={id} value={id}>{name} - {hint}</option>)}</select>
            <button disabled={busy || config.role !== "operator"} onClick={() => void simulate()}>{busy ? "Submitting..." : realScenario ? "Diagnose live pods" : "+ Simulate incident"}</button>
          </section>
          <div className="operations-grid">
            <section className="panel queue"><div className="section-title"><h2>Incident queue</h2><span className="badge">{incidents.length}</span></div>
              <input aria-label="Filter incidents" placeholder="Filter by service, status or title" value={filter} onChange={e => setFilter(e.target.value)} />
              {filtered.length === 0 && <div className="empty">No incidents yet. Start with cache pressure.</div>}
              {filtered.map(item => <button key={item.id} className={`incident ${selected === item.id ? "selected" : ""}`} onClick={() => setSelected(item.id)}>
                <div><span className={`severity ${item.severity}`} /> <strong>{item.title}</strong></div>
                <div className="incident-meta"><span>{item.service}</span><Status status={item.status} /></div>
                <small>{new Date(item.created_at).toLocaleString()}</small>
              </button>)}
            </section>
            <section className="panel detail"><div className="section-title"><h2>Live execution trace</h2><span className={`stream ${streamState === "live" ? "live" : ""}`}>{streamState}</span></div>
              {!current ? <div className="empty tall">Select an incident to inspect its evidence, decisions and recovery.</div> : <>
                <div className="detail-heading"><h3>{current.title}</h3><Status status={current.status} /></div>
                <small className="mono">{current.id}</small>
                {(streamState === "disconnected" || streamState === "complete") && !terminal.has(current.status) &&
                  <button className="secondary" onClick={() => setStreamRevision(value => value + 1)}>Reconnect live trace</button>}
                <RemediationProgress incident={current} events={events} />
                {current.error && <div className="error">{current.error}</div>}
                {current.plan && <div className="recommendation"><div className="eyebrow">AGENT RECOMMENDATION</div>
                  <h3>{label(current.plan.action)} <span>{(current.plan.confidence * 100).toFixed(0)}% confidence</span></h3>
                  <p>{current.plan.summary}</p><small>Evidence: {current.plan.evidence_ids.join(" / ") || "none"}</small>
                </div>}
                {current.scenario.startsWith("k8s_") && events.filter(event => event.stage === "kubernetes_plan").slice(-1).map(event =>
                  <div className="recommendation" key={event.seq}><h3>Exact Kubernetes change for approval</h3>
                    <p>Includes the namespace, target, bounded field change and rollback checkpoint. Changed targets invalidate approval.</p>
                    <pre>{JSON.stringify(event.payload.change, null, 2)}</pre>
                  </div>)}
                {repositoryMode && repositoryPlan && <div className="recommendation">
                  <h3>Repository manifest diff for owner approval</h3>
                  <p>Only the four demo Deployment configurations are eligible. YAML formatting is normalized;
                    the permitted semantic change is limited to the proposed field.</p>
                  <pre>{typeof repositoryPlan.payload.plan === "object" && repositoryPlan.payload.plan !== null
                    && "diff" in repositoryPlan.payload.plan && typeof repositoryPlan.payload.plan.diff === "string"
                    ? repositoryPlan.payload.plan.diff : "Repository diff is unavailable; do not approve."}</pre>
                </div>}
                {repositoryMode && typeof pullRequestUrl === "string"
                  && pullRequestUrl.startsWith(`https://github.com/${config.github_gitops?.repository}/pull/`) &&
                  <div className="recommendation"><h3>Owner-approved remediation PR</h3>
                    <a href={pullRequestUrl} target="_blank" rel="noopener noreferrer">Open pull request for manual review and merge</a>
                    <p>Approval is not deployment. Waiting for human merge and merge-commit CI before cluster changes.</p>
                  </div>}
                {current.status === "awaiting_approval" && <div className="approval"><h3>Accountable owner approval required</h3>
                  <p>{repositoryMode ? "Approve the exact repository diff to create a PR. This does not merge or deploy it."
                    : "Approve the exact persisted proposal. Operators cannot approve their own changes."}</p>
                  {repositoryMode && !repositoryPlan && <p role="alert">
                    This legacy proposal has no repository diff. Approval is blocked.
                    Reject it, then diagnose again as an operator after the integration manifests are published on main.
                  </p>}
                  {config.role === "owner" ? <><textarea aria-label="Approval reason" placeholder="Record your review and justification (8+ characters)" minLength={8} maxLength={500} value={reason} onChange={e => setReason(e.target.value)} />
                    <div className="actions"><button disabled={busy || reason.trim().length < 8 || (repositoryMode && !repositoryPlan)} onClick={() => void decide("approve")}>{repositoryMode ? "Approve & create PR" : "Approve change"}</button>
                      <button className="secondary" disabled={busy || reason.trim().length < 8} onClick={() => void decide("reject")}>Reject</button></div></>
                    : <p className="muted">Enter the application OWNER_TOKEN in Access token and click Connect.
                      Do not enter a GitHub access token, App ID or private key. The selected incident is retained.</p>}
                </div>}
                <ol className="timeline" aria-live="polite">{events.map(event => <li key={event.seq}>
                  <div className="event-top"><b>{event.stage.toUpperCase()}</b><time>{new Date(event.at).toLocaleTimeString()}</time></div>
                  <p>{event.message}</p><small>{event.actor}</small>
                  {Object.keys(event.payload).length > 0 && <details><summary>Evidence &amp; telemetry</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}
                </li>)}</ol>
                <button className="secondary" onClick={exportAudit}>Export audit JSON</button>
                {terminal.has(current.status) && <small className="muted"> Workflow complete. Evidence is retained in the audit store.</small>}
              </>}
            </section>
          </div>
        </>}
        {tab === "Runbooks" && <section className="cards">{runbooks.map(book => <article className="panel" key={book.id}>
          <div className="section-title"><span className="mono">{book.id}</span><span className="badge">{book.risk} risk</span></div>
          <h2>{label(book.action)}</h2><p>{book.description}</p><h3>Rollback</h3><p>{book.rollback}</p>
          <small>Version {config.runbook_version} / {book.id.startsWith("RB-K8S") ? "real Kubernetes / owner approval" : "simulator-v1"} / reviewed policy</small>
        </article>)}</section>}
        {tab === "Observability" && <section className="panel"><h2>Inference &amp; governance telemetry</h2>
          <p>Provider-specific timings, token usage, estimated cost and prompt version are recorded on every successful inference step. Failures appear explicitly in the audit trail and metrics.</p>
          <p>Calls with unavailable token usage: {telemetry?.calls_with_missing_usage ?? 0}. Totals include reported usage only.
            Copilot subscription usage is not a dollar invoice. No token counts are guessed when the SDK omits them.</p>
          <div className="stats"><Metric label="LLM CALLS" value={String(telemetry?.llm_calls ?? 0)} note={config.model} />
            <Metric label="INPUT TOKENS" value={String(telemetry?.input_tokens ?? 0)} note="Provider-reported usage" />
            <Metric label="OUTPUT TOKENS" value={String(telemetry?.output_tokens ?? 0)} note="Provider-reported usage" /></div>
          <h3>Outcome distribution</h3>{Object.entries(telemetry?.outcomes ?? {}).map(([status, count]) =>
            <div className="bar-row" key={status}><span>{label(status)}</span><meter min="0" max={Math.max(1, telemetry?.incident_count ?? 1)} value={count} /><b>{count}</b></div>)}
          <p>Prompt: <code>{config.prompt_version}</code> / Max agent steps: {config.max_agent_steps} / Auto confidence threshold: {config.confidence_threshold}</p>
          <p className="muted">Prometheus metrics are exposed on the authenticated /api/metrics endpoint. Full evaluations are generated with the offline evaluation command; no API benchmark numbers are invented.</p>
        </section>}
        {tab === "Learning" && <section className="panel"><h2>Feedback without unsafe self-modification</h2>
          <p>Outcomes become runbook review candidates, not automatically deployed changes. Refresh to load newly completed outcomes.</p>
          <button className="secondary" onClick={() => void api<Feedback[]>("/feedback").then(setFeedback).catch((err: Error) => setError(err.message))}>Refresh feedback</button>
          {feedback.length === 0 && <div className="empty">No recovery outcomes recorded yet.</div>}
          {feedback.map(item => <article className="feedback" key={item.incident_id}><Status status={item.outcome} />
            <p>{item.candidate}</p><small className="mono">{item.incident_id}</small></article>)}
        </section>}
      </>}
      <footer>RemediAI Copilot API / Evidence before action / {!config ? "Connect to view execution scope" : config.kubernetes?.enabled
        ? `Real Kubernetes: ${config.kubernetes.namespace} / Other scenarios remain simulated`
        : "Synthetic data and simulated infrastructure"}</footer>
    </main>
  </div>;
}

function RemediationProgress({ incident, events }: { incident: Incident; events: AuditEvent[] }) {
  const real = incident.scenario.startsWith("k8s_");
  const repositoryMode = events.some(event => ["repository_plan", "github_pr", "merge"].includes(event.stage))
    || ["awaiting_merge", "awaiting_ci"].includes(incident.status);
  const phases = repositoryMode
    ? ["Repository + live evidence", "AI review + manifest diff", "Owner approval", "PR + human merge", "Merge-commit CI", "Deploy", "Verify recovery", "Outcome"]
    : ["Evidence + AI diagnosis", "AI review + policy", "Owner approval", "Apply change", "Verify recovery", "Outcome"];
  const latest = events.filter(event => event.stage === "progress").at(-1);
  const stopped = terminal.has(incident.status);
  const index = stopped ? phases.length - 1 : incident.status === "verifying" ? (repositoryMode ? 6 : 4)
    : incident.status === "executing" ? (repositoryMode ? 5 : 3)
    : incident.status === "awaiting_ci" ? 4 : incident.status === "awaiting_merge" ? 3
    : ["awaiting_approval", "approved"].includes(incident.status) ? 2
    : latest?.payload.phase === "review" ? 1 : 0;
  const passes = latest?.payload.consecutive_passes;
  const recorded = [
    events.some(event => ["triage", "observe", "reason"].includes(event.stage)),
    events.some(event => ["judge", "decision"].includes(event.stage)),
    incident.status === "awaiting_approval" || events.some(event => event.stage === "approval"),
    ...(repositoryMode ? [
      events.some(event => event.stage === "github_pr"),
      events.some(event => event.stage === "merge"),
    ] : []),
    events.some(event => event.stage === "execute"),
    events.some(event => event.stage === "verify"),
    stopped,
  ];
  return <section className="remediation-progress" aria-label="Remediation progress">
    <div className="section-title"><h3>Remediation progress</h3>
      <span className="badge">{real ? "LIVE KUBERNETES" : "SIMULATED ACTIONS"}</span></div>
    <ol>{phases.map((phase, position) => <li key={phase}
      className={position === index ? "current" : recorded[position] ? "past" : "pending"}
      aria-current={position === index ? "step" : undefined}>
      <span>{position + 1}</span>{phase}
    </li>)}</ol>
    <p role="status">{incident.status === "awaiting_approval"
      ? "Paused: owner approval required. No remediation patch has been applied."
      : incident.status === "awaiting_merge" ? "PR created. Waiting for a human to merge; no cluster change applied."
      : incident.status === "awaiting_ci" ? "Merged. Waiting for successful CI on the exact merge commit; no cluster change applied."
      : incident.status === "closed" ? "Recovery verified; incident closed."
      : incident.status === "rolled_back" ? "Rollback recorded. Review the audit outcome; recovery is not established."
      : incident.status === "escalated" ? "Escalated without remediation; operator investigation required."
      : incident.status === "rejected" ? "Owner rejected the proposal; no remediation was applied."
      : incident.status === "failed" ? "Workflow stopped on error. Inspect the audit trail and actual workload state."
      : incident.status === "executing" ? "Applying the approved change; waiting for the adapter response."
      : incident.status === "verifying" && latest?.payload.phase !== "verification" ? "Watching rollout and collecting recovery checks."
      : latest?.message ?? (incident.status === "queued" ? "Queued for evidence collection." : "Workflow in progress.")}</p>
    {real && incident.status === "verifying" && typeof passes === "number" &&
      <p>Measured consecutive recovery checks: <strong>{passes} / 3</strong></p>}
    <small>Stages reflect recorded workflow activity, not a time estimate or a fabricated completion percentage.
      {stopped && " Reaching an outcome does not necessarily mean recovery."}</small>
  </section>;
}

function Status({ status }: { status: string }) {
  return <span className={`status ${status}`}>{label(status)}</span>;
}
function Metric({ label: title, value, note }: { label: string; value: string; note: string }) {
  return <article className="metric panel"><div className="eyebrow">{title}</div><strong>{value}</strong><small>{note}</small></article>;
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
