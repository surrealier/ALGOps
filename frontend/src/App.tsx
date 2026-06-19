import { useEffect, useRef, useState } from "react";
import { api, openWs, type Experiment, type Project } from "./api";

const PK = "detector.confidence_threshold";
const METRICS = [
  { v: "fp_rate", label: "fp_rate (오탐율↓)" },
  { v: "fn_rate", label: "fn_rate (미탐율↓)" },
  { v: "precision", label: "precision↑" },
  { v: "recall", label: "recall↑" },
  { v: "f1", label: "f1↑" },
];
type Row = { iter: number; params: any; metrics: any; decision?: string };

function statusClass(s: string) {
  if (s === "running") return "run";
  if (s === "failed") return "fail";
  if (s === "paused") return "warn";
  if (s === "target_reached" || s.startsWith("stopped")) return "done";
  return "";
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [exp, setExp] = useState<Experiment | null>(null);
  const [rows, setRows] = useState<Record<number, Row>>({});
  const [final, setFinal] = useState<any>(null);
  const [approval, setApproval] = useState<any>(null);
  const [healthy, setHealthy] = useState(false);
  const [form, setForm] = useState({ goal_text: "오탐 완화", target_metric: "fp_rate", target_value: 0.05, max_iteration: 10 });
  const wsRef = useRef<WebSocket | null>(null);

  const loadProjects = () => api.projects().then(setProjects).catch(() => {});
  useEffect(() => {
    api.health().then(() => setHealthy(true)).catch(() => setHealthy(false));
    loadProjects();
    const t = setInterval(loadProjects, 6000);
    const h = setInterval(() => api.health().then(() => setHealthy(true)).catch(() => setHealthy(false)), 8000);
    return () => { clearInterval(t); clearInterval(h); };
  }, []);

  const selectProject = async (p: Project) => {
    setProject(p); setExp(null); setFinal(null); setRows({});
    setExperiments(await api.experiments(p.project_id));
  };

  const createProject = async (name: string) => {
    if (!name.trim()) return;
    const p = await api.createProject(name.trim());
    await loadProjects(); selectProject(p);
  };

  const openExperiment = async (eid: string) => {
    const e = await api.experiment(eid);
    setExp(e); setFinal(null); setApproval(null); setRows({});
    const existing = await api.runs(eid);
    const r: Record<number, Row> = {};
    existing.forEach((x: any) => (r[x.iteration_index] = { iter: x.iteration_index, params: x.param_values, metrics: x.metrics }));
    setRows(r);
    if (["target_reached", "stopped_max_iter", "stopped_by_pm"].includes(e.status)) api.final(eid).then(setFinal).catch(() => {});
    wsRef.current?.close();
    wsRef.current = openWs(eid, handleEvent);
  };

  const handleEvent = (m: any) => {
    if (m.type === "run_evaluated") setRows((r) => ({ ...r, [m.iteration]: { iter: m.iteration, params: m.params, metrics: m.metrics } }));
    else if (m.type === "iteration_decided") setRows((r) => (r[m.iteration] ? { ...r, [m.iteration]: { ...r[m.iteration], decision: m.pm_action } } : r));
    else if (m.type === "approval_required") { setApproval(m); setExp((e) => (e ? { ...e, status: "paused" } : e)); }
    else if (m.type === "experiment_started") setExp((e) => (e ? { ...e, status: "running" } : e));
    else if (m.type === "experiment_done") { setExp((e) => (e ? { ...e, status: m.status } : e)); setApproval(null); api.final(m.experiment_id).then(setFinal).catch(() => {}); loadProjects(); }
    else if (m.type === "experiment_failed") setExp((e) => (e ? { ...e, status: "failed" } : e));
  };

  const createExperiment = async () => {
    if (!project) return;
    const e = await api.createExperiment({
      project_id: project.project_id, name: "실험-" + new Date().toLocaleTimeString("ko-KR"),
      ...form, target_value: Number(form.target_value), max_iteration: Number(form.max_iteration),
    });
    setExperiments(await api.experiments(project.project_id));
    openExperiment(e.experiment_id);
  };

  const dir = (d: string) => (d === "minimize" ? "≤" : "≥");
  const cells = (m: any) => (m ? [m.precision, m.recall, m.f1, m.fp_rate, m.fn_rate].map((x, i) => <td key={i}>{x.toFixed(3)}</td>) : <td colSpan={5}>–</td>);
  const sortedRows = Object.values(rows).sort((a, b) => a.iter - b.iter);

  return (
    <div>
      <header>
        <h1>⚙️ ALGOps</h1>
        <span className="sub">알고리즘 파라미터 최적화</span>
        <span style={{ flex: 1 }} />
        <span className="sub" style={{ color: healthy ? "var(--ok)" : "var(--bad)" }}>●</span>
      </header>
      <div className="wrap">
        <div className="col left">
          <div className="card">
            <h2>프로젝트</h2>
            {projects.length === 0 && <div className="empty">아직 프로젝트가 없습니다.</div>}
            {projects.map((p) => (
              <div key={p.project_id} className={"item" + (project?.project_id === p.project_id ? " sel" : "")} onClick={() => selectProject(p)}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <b>{p.name}</b>
                  <span className={"pill " + (p.slave_online ? "on" : "")}>{p.slave_online ? "● online" : "○ offline"}</span>
                </div>
                <div className="muted sm">{p.modality}</div>
              </div>
            ))}
            <NewProject onCreate={createProject} />
          </div>
          {project && (
            <div className="card">
              <h2>실험 — {project.name}</h2>
              {experiments.map((e) => (
                <div key={e.experiment_id} className={"item" + (exp?.experiment_id === e.experiment_id ? " sel" : "")} onClick={() => openExperiment(e.experiment_id)}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <span>{e.name}</span><span className={"pill " + statusClass(e.status)}>{e.status}</span>
                  </div>
                  <div className="muted sm">{e.goal_text} · {e.target_metric} {dir(e.goal_direction)} {e.target_value}</div>
                </div>
              ))}
              <div style={{ marginTop: 10 }}>
                <label>목표 (자연어)</label>
                <input value={form.goal_text} onChange={(ev) => setForm({ ...form, goal_text: ev.target.value })} />
                <div className="row">
                  <div style={{ flex: 1 }}>
                    <label>목표 지표</label>
                    <select value={form.target_metric} onChange={(ev) => setForm({ ...form, target_metric: ev.target.value })}>
                      {METRICS.map((m) => <option key={m.v} value={m.v}>{m.label}</option>)}
                    </select>
                  </div>
                  <div style={{ width: 90 }}><label>목표값</label><input value={form.target_value} onChange={(ev) => setForm({ ...form, target_value: ev.target.value as any })} /></div>
                  <div style={{ width: 80 }}><label>max iter</label><input value={form.max_iteration} onChange={(ev) => setForm({ ...form, max_iteration: ev.target.value as any })} /></div>
                </div>
                <button style={{ marginTop: 10, width: "100%" }} onClick={createExperiment}>실험 생성</button>
              </div>
            </div>
          )}
        </div>

        <div className="col right">
          {!exp && <div className="empty">프로젝트와 실험을 선택하세요.</div>}
          {exp && (
            <>
              <div className="card">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <div>
                    <div className="muted">실험</div>
                    <div style={{ fontSize: 18, fontWeight: 650 }}>{exp.name}</div>
                    <div className="muted sm">목표: {exp.goal_text} · <code>{exp.target_metric}</code> {dir(exp.goal_direction)} {exp.target_value} · 최대 {exp.max_iteration} iter</div>
                  </div>
                  <div className="row">
                    <span className={"pill " + statusClass(exp.status)}>{exp.status}</span>
                    <button disabled={exp.status === "running"} onClick={() => { setFinal(null); api.run(exp.experiment_id); }}>▶ 실행</button>
                  </div>
                </div>
              </div>

              {approval && (
                <div className="card" style={{ borderColor: "var(--warn)" }}>
                  <h2>승인 대기 (HITL)</h2>
                  <div className="sm">iteration {approval.iteration} — 제안 파라미터: <code>{JSON.stringify(approval.proposed_params)}</code></div>
                  <div className="row" style={{ marginTop: 8 }}>
                    <button onClick={() => { api.approve(exp.experiment_id, true); setApproval(null); }}>승인</button>
                    <button className="ghost" onClick={() => { api.approve(exp.experiment_id, false); setApproval(null); }}>거절</button>
                  </div>
                </div>
              )}

              <div className="card">
                <h2>진행 (반복)</h2>
                <table>
                  <thead><tr><th>iter</th><th>threshold</th><th>precision</th><th>recall</th><th>f1</th><th>fp_rate</th><th>fn_rate</th><th>판정</th></tr></thead>
                  <tbody>
                    {sortedRows.length === 0 && <tr><td colSpan={8} className="empty">실행하면 반복이 표시됩니다.</td></tr>}
                    {sortedRows.map((r) => (
                      <tr key={r.iter}>
                        <td>{r.iter}</td>
                        <td>{r.params?.[PK] != null ? Number(r.params[PK]).toFixed(3) : "–"}</td>
                        {cells(r.metrics)}
                        <td>{r.decision ? <span className={"pill " + (r.decision === "stop" ? "done" : "")}>{r.decision}</span> : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {final && (
                <div className="card">
                  <h2>최종 결과</h2>
                  <div className="row" style={{ gap: 24, alignItems: "flex-end" }}>
                    <div><div className="muted">종료 사유</div><div style={{ fontSize: 18, fontWeight: 700 }}>{final.stop_reason || "-"}</div></div>
                    <div><div className="muted">최적 {exp.target_metric}</div><div className="big">{final.metrics ? final.metrics[exp.target_metric].toFixed(3) : "-"}</div></div>
                    <div style={{ flex: 1 }}>
                      <div className="muted">최적 파라미터</div>
                      {Object.entries(final.optimal_params || {}).map(([k, v]) => <div key={k} className="kv"><code>{k}</code><b>{typeof v === "number" ? (v as number).toFixed(3) : String(v)}</b></div>)}
                    </div>
                  </div>
                  <div className="muted sm" style={{ marginTop: 8 }}>알림 발송: {final.notified ? "완료" : "미발송"} · 우승 run <code>{(final.winning_run_id || "").slice(0, 8)}</code></div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function NewProject({ onCreate }: { onCreate: (n: string) => void }) {
  const [name, setName] = useState("");
  return (
    <div className="row" style={{ marginTop: 8 }}>
      <input placeholder="새 프로젝트 이름" value={name} onChange={(e) => setName(e.target.value)} />
      <button className="ghost" onClick={() => { onCreate(name); setName(""); }}>+ 추가</button>
    </div>
  );
}
