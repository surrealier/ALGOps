// Thin client for the master API (same-origin; the master serves the built UI at "/").
// When the master has an API token, every request (reads, mutations, WebSockets, artifact links)
// carries it — from localStorage, entered once via the token dialog.

export type Metrics = Record<string, number>;

export type Project = {
  project_id: string; name: string; modality: string; description: string | null;
  slave_online: boolean; repo_kind: "git" | "local"; repo_url: string | null; default_ref: string | null;
  watch: boolean; latest_sha: string | null; synced_at: string | null;
  algorithms: string[]; suites: string[];
};

export type Constraint = { metric: string; op: ">=" | "<=" | ">" | "<" | "=="; value: number };
export type Objective = { metric: string; direction: "minimize" | "maximize"; target: number | null; constraints: Constraint[] };

export type ParamDef = {
  key: string; dtype: "float" | "int" | "bool" | "enum"; default: unknown;
  range?: [number, number] | null; step?: number | null; enum?: unknown[] | null;
  stage: string; description?: string | null;
};
export type AlgorithmInfo = {
  id: string; description: string | null; params: number; suites: string[];
  metrics: Record<string, "minimize" | "maximize">; objective: Objective | null;
  gates: Constraint[]; runs_on: string[];
};
export type ManifestOut = {
  sha: string; ref: string | null; dirty: boolean; synced_at: string; valid: boolean;
  manifest: { name?: string; suites: { id: string; uri: string; tags: string[] }[];
    algorithms: { id: string; param_spec: { params: ParamDef[] } | null }[] };
  algorithms?: AlgorithmInfo[];
};
export type Revision = { revision_id: string; project_id: string; ref: string | null; sha: string; dirty: boolean; synced_at: string };

export type Experiment = {
  experiment_id: string; project_id: string; name: string; goal_text: string;
  target_metric: string; target_value: number | null; goal_direction: string; max_iteration: number;
  status: string; best_run_id: string | null; algorithm_id: string; suite_id: string;
  validation_suite_ids: string[]; objective: Objective | null; strategy: string;
  search_keys: string[] | null; batch_size: number; ref: string | null; commit_sha: string | null;
  attempt: number; created_at: string | null;
};
export type Run = {
  run_id: string; experiment_id: string | null; iteration_index: number; status: string;
  param_values: Record<string, unknown>; metrics: Metrics | null; score: number | null;
  summary_md_uri: string | null; benchmark_id: string | null; algorithm_id: string; suite_id: string;
  commit_sha: string | null; kind: string; duration_s: number | null; error: string | null;
  log_uri: string | null; attempt: number;
};
export type FinalResult = {
  experiment_id: string; winning_run_id: string | null; optimal_params: Record<string, unknown>;
  metrics: Metrics | null; stop_reason: string | null; report_md_uri: string | null; notified: boolean;
  validation: Record<string, { best: any; baseline: any; improved: boolean | null }> | null;
  changed_params: Record<string, { old: unknown; new: unknown }>;
  promotion: Promotion | null;
};
export type Promotion = {
  ok: boolean; branch?: string; commit_sha?: string; base_sha?: string; pushed?: boolean;
  changed?: Record<string, { old: unknown; new: unknown }>; files?: string[]; patch_uri?: string;
  pr_url?: string | null; pr_error?: string; error?: string | null;
};

export type Gate = { metric: string; op: string; value: number; actual: number | null; passed: boolean };
export type Delta = { current: number | null; baseline: number | null; delta: number | null; tolerance: number | null; direction: string; status: string };
export type Cell = {
  cell_id: string; benchmark_id: string; project_id: string; project_name: string;
  algorithm_id: string; suite_id: string; commit_sha: string | null; run_id: string | null;
  verdict: string; metrics: Metrics | null; gates: Gate[]; baseline_run_id: string | null;
  baseline_sha: string | null; deltas: Record<string, Delta>; error: string | null; duration_s: number | null;
};
export type BenchmarkScope = { project_id: string; ref?: string; algorithms?: string[]; suites?: string[]; tags?: string[] };
export type Benchmark = {
  benchmark_id: string; name: string; status: string; trigger: string; created_at: string;
  finished_at: string | null; scope: any[] | null; summary: Record<string, number>;
  revisions: Record<string, string>; cells: Cell[] | null;
};
export type Runner = {
  slave_id: string; host_label: string; status: string; online: boolean; labels: string[];
  concurrency: number; inflight: number; git: boolean; local_projects: string[];
  allow_projects: string[] | null; last_heartbeat: string | null; version: string;
};
export type Schedule = {
  schedule_id: string; name: string; kind: "cron" | "watch"; cron: string | null; interval_s: number;
  scope: any[] | null; enabled: boolean; last_fired_at: string | null; next_fire_at: string | null;
  last_benchmark_id: string | null;
};
export type Health = { status: string; dev_mode: boolean; version: string; auth: boolean };

const TOKEN_KEY = "algops.api_token";
export const token = {
  get: (): string => { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } },
  set: (v: string) => { try { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ } },
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function j<T>(url: string, opts: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers as Record<string, string> || {}) };
  const t = token.get();
  if (t) headers["authorization"] = `Bearer ${t}`;
  if (opts.body) headers["content-type"] = "application/json";
  const r = await fetch(url, { ...opts, headers });
  if (!r.ok) {
    let msg = await r.text();
    try { msg = JSON.parse(msg).detail ?? msg; } catch { /* plain text */ }
    throw new ApiError(r.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get("content-type") || "";
  return (ct.includes("json") ? await r.json() : await r.text()) as T;
}
const post = <T,>(url: string, body?: unknown) => j<T>(url, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
const q = (o: Record<string, unknown>) => {
  const p = new URLSearchParams();
  Object.entries(o).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") p.set(k, String(v)); });
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  health: () => j<Health>("/healthz"),
  // projects
  projects: () => j<Project[]>("/api/v1/projects"),
  project: (id: string) => j<Project>(`/api/v1/projects/${encodeURIComponent(id)}`),
  createProject: (body: { name: string; repo_url?: string | null; default_ref?: string | null; watch?: boolean; description?: string }) =>
    post<Project>("/api/v1/projects", body),
  updateProject: (id: string, body: Partial<Pick<Project, "watch" | "default_ref" | "description">>) =>
    j<Project>(`/api/v1/projects/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  sync: (id: string, ref?: string) => post<Revision>(`/api/v1/projects/${id}/sync${q({ ref })}`),
  revisions: (id: string) => j<Revision[]>(`/api/v1/projects/${id}/revisions`),
  manifest: (id: string) => j<ManifestOut>(`/api/v1/projects/${id}/manifest`),
  history: (id: string, algorithm_id: string, suite_id: string) =>
    j<Run[]>(`/api/v1/projects/${id}/history${q({ algorithm_id, suite_id })}`),
  // benchmarks
  benchmarks: (project_id?: string) => j<Benchmark[]>(`/api/v1/benchmarks${q({ project_id, limit: 50 })}`),
  benchmark: (id: string) => j<Benchmark>(`/api/v1/benchmarks/${id}`),
  createBenchmark: (body: { name: string; scope?: any[] | null; baseline?: string }) => post<Benchmark>("/api/v1/benchmarks", body),
  accept: (id: string, cell_id?: string) => post<{ accepted: number }>(`/api/v1/benchmarks/${id}/accept${q({ cell_id })}`),
  // experiments
  experiments: (project_id?: string) => j<Experiment[]>(`/api/v1/experiments${q({ project_id })}`),
  experiment: (id: string) => j<Experiment>(`/api/v1/experiments/${id}`),
  createExperiment: (body: Record<string, unknown>) => post<Experiment>("/api/v1/experiments", body),
  runs: (id: string) => j<Run[]>(`/api/v1/experiments/${id}/runs`),
  final: (id: string) => j<FinalResult>(`/api/v1/experiments/${id}/final`),
  run: (id: string) => post<{ attempt: number }>(`/api/v1/experiments/${id}/run`),
  approve: (id: string, approved: boolean) => post(`/api/v1/experiments/${id}/approve${q({ approved })}`),
  promote: (id: string, body: { push?: boolean; pull_request?: boolean }) => post<Promotion>(`/api/v1/experiments/${id}/promote`, body),
  // runners, schedules, artifacts
  runners: () => j<Runner[]>("/api/v1/slaves"),
  schedules: () => j<Schedule[]>("/api/v1/schedules"),
  createSchedule: (body: Record<string, unknown>) => post<Schedule>("/api/v1/schedules", body),
  toggleSchedule: (id: string, enabled: boolean) => j<Schedule>(`/api/v1/schedules/${id}${q({ enabled })}`, { method: "PATCH" }),
  fireSchedule: (id: string) => post<{ benchmark_id?: string; changed?: boolean }>(`/api/v1/schedules/${id}/fire`),
  deleteSchedule: (id: string) => j(`/api/v1/schedules/${id}`, { method: "DELETE" }),
  artifact: (uri: string) => j<string>(`/api/v1/artifacts${q({ uri })}`),
  artifactUrl: (uri: string) => `/api/v1/artifacts${q({ uri, token: token.get() })}`,
};

export function openWs(channel: "experiments" | "benchmarks", id: string, onMsg: (m: any) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${channel}/${id}${q({ token: token.get() })}`);
  ws.onmessage = (e) => { try { onMsg(JSON.parse(e.data)); } catch { /* ignore */ } };
  return ws;
}
