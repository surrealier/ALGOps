// Thin client for the master API. Same-origin in production (served from dist).

export type Project = { project_id: string; name: string; modality: string; slave_online: boolean };
export type Experiment = {
  experiment_id: string; project_id: string; name: string; goal_text: string;
  target_metric: string; target_value: number; goal_direction: string; max_iteration: number;
  status: string; best_run_id: string | null;
};
export type Metrics = {
  precision: number; recall: number; f1: number; fp_rate: number; fn_rate: number;
  tp: number; fp: number; fn: number; tn: number;
};

async function j<T>(url: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.text()) || String(r.status));
  return r.status === 204 ? (null as T) : ((await r.json()) as T);
}
const POST = { method: "POST", headers: { "content-type": "application/json" } };

export const api = {
  health: () => j<{ status: string }>("/healthz"),
  projects: () => j<Project[]>("/api/v1/projects"),
  createProject: (name: string) =>
    j<Project>("/api/v1/projects", { ...POST, body: JSON.stringify({ name }) }),
  experiments: (pid: string) => j<Experiment[]>(`/api/v1/experiments?project_id=${pid}`),
  createExperiment: (body: Record<string, unknown>) =>
    j<Experiment>("/api/v1/experiments", { ...POST, body: JSON.stringify(body) }),
  experiment: (eid: string) => j<Experiment>(`/api/v1/experiments/${eid}`),
  runs: (eid: string) => j<any[]>(`/api/v1/experiments/${eid}/runs`),
  final: (eid: string) => j<any>(`/api/v1/experiments/${eid}/final`),
  run: (eid: string) => j(`/api/v1/experiments/${eid}/run`, { method: "POST" }),
  approve: (eid: string, approved: boolean) =>
    j(`/api/v1/experiments/${eid}/approve?approved=${approved}`, { method: "POST" }),
};

export function openWs(eid: string, onMsg: (m: any) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/experiments/${eid}`);
  ws.onmessage = (e) => onMsg(JSON.parse(e.data));
  return ws;
}
