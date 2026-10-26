import { useState } from "react";
import { api, type AlgorithmInfo, type Project } from "../api";
import { go } from "../nav";
import { act, ago, Card, Empty, Field, fmt, opText, Sha, Sparkline, StatusPill, useAsync } from "../ui";
import { NewBenchmark, SummaryChips } from "./benchmarks";

function RegisterProject({ onCreated }: { onCreated: (p: Project) => void }) {
  const [f, setF] = useState({ name: "", repo_url: "", default_ref: "", watch: true });
  const submit = async () => {
    if (!f.name.trim() || !f.repo_url.trim()) return;
    const p = await act(() => api.createProject({
      name: f.name.trim(), repo_url: f.repo_url.trim(), default_ref: f.default_ref.trim() || null, watch: f.watch,
    }), "프로젝트를 등록했습니다");
    if (!p) return;
    await act(() => api.sync(p.project_id), "매니페스트를 동기화했습니다");
    onCreated(p);
  };
  return (
    <div className="form-grid">
      <Field label="이름"><input value={f.name} placeholder="vision-postproc" onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      <Field label="Git URL" hint="Runner가 clone 할 수 있는 주소"><input value={f.repo_url} placeholder="git@gitlab.corp:vision/postproc.git" onChange={(e) => setF({ ...f, repo_url: e.target.value })} /></Field>
      <Field label="기본 ref" hint="비우면 원격 기본 브랜치"><input value={f.default_ref} placeholder="main" onChange={(e) => setF({ ...f, default_ref: e.target.value })} /></Field>
      <label className="check"><input type="checkbox" checked={f.watch} onChange={(e) => setF({ ...f, watch: e.target.checked })} /> 새 커밋 감지 시 자동 벤치마크</label>
      <div><button onClick={submit}>등록 + 동기화</button></div>
      <p className="muted sm span2">로컬 경로 프로젝트는 Runner를 <code>ALGOPS_SLAVE_LOCAL_PROJECTS="name=/path"</code> 로 띄우면 자동 등록됩니다.</p>
    </div>
  );
}

export function ProjectsView() {
  const list = useAsync(() => api.projects(), [], 6000);
  const [adding, setAdding] = useState(false);
  return (
    <div className="page">
      <Card title="프로젝트 (레포지토리)" actions={<button onClick={() => setAdding((v) => !v)}>{adding ? "닫기" : "+ Git 레포 등록"}</button>}>
        {adding && <RegisterProject onCreated={(p) => { setAdding(false); list.reload(); go(`/projects/${p.project_id}`); }} />}
        {list.data?.length === 0 && <Empty>등록된 프로젝트가 없습니다. Git 레포를 등록하거나 Runner를 로컬 프로젝트와 함께 실행하세요.</Empty>}
        {!!list.data?.length && (
          <table className="list">
            <thead><tr><th>이름</th><th>종류</th><th>알고리즘</th><th>스위트</th><th>최신 커밋</th><th>Runner</th></tr></thead>
            <tbody>{list.data.map((p) => (
              <tr key={p.project_id} className="click" onClick={() => go(`/projects/${p.project_id}`)}>
                <td><b>{p.name}</b>{p.watch && <span className="pill acc sm-pill" title="새 커밋 감지">watch</span>}</td>
                <td className="muted">{p.repo_kind === "git" ? "git" : "로컬"}</td>
                <td>{p.algorithms.length ? p.algorithms.join(", ") : <span className="muted">미동기화</span>}</td>
                <td>{p.suites.length}</td>
                <td><Sha sha={p.latest_sha} /> <span className="muted sm">{ago(p.synced_at)}</span></td>
                <td>{p.slave_online ? <span className="pill ok">● 연결</span> : <span className="pill bad">○ 없음</span>}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

function CellTrend({ pid, algo, suite, metric }: { pid: string; algo: string; suite: string; metric: string }) {
  const h = useAsync(() => api.history(pid, algo, suite), [pid, algo, suite]);
  const values = (h.data || []).slice().reverse().map((r) => r.metrics?.[metric] ?? null);
  const last = values.length ? values[values.length - 1] : null;
  return <span className="row gap-s"><Sparkline values={values} /><span className="sm">{fmt(last, 3)}</span></span>;
}

function AlgorithmCard({ pid, a }: { pid: string; a: AlgorithmInfo }) {
  const metric = a.objective?.metric || a.gates[0]?.metric || Object.keys(a.metrics)[0];
  return (
    <div className="algo">
      <div className="row gap wrap">
        <b>{a.id}</b><span className="muted sm">{a.description}</span>
        <span style={{ flex: 1 }} />
        <span className="muted sm">파라미터 {a.params}개</span>
        {a.runs_on.length > 0 && <span className="muted sm">runs_on: {a.runs_on.join(", ")}</span>}
      </div>
      <div className="row gap wrap sm" style={{ margin: "6px 0" }}>
        {a.objective && <span>목표 <code>{a.objective.metric} {a.objective.direction === "minimize" ? "↓" : "↑"}{a.objective.target != null ? ` (${a.objective.direction === "minimize" ? "≤" : "≥"} ${a.objective.target})` : ""}</code></span>}
        {a.objective?.constraints.map((c) => <span key={c.metric}>제약 <code>{c.metric} {opText(c.op)} {c.value}</code></span>)}
        {a.gates.map((g) => <span key={g.metric + g.op}>게이트 <code>{g.metric} {opText(g.op)} {g.value}</code></span>)}
      </div>
      {metric && (
        <table className="compact">
          <thead><tr><th>스위트</th><th>{metric} 추이 (벤치마크)</th><th></th></tr></thead>
          <tbody>{a.suites.map((s) => (
            <tr key={s}><td>{s}</td><td><CellTrend pid={pid} algo={a.id} suite={s} metric={metric} /></td>
              <td><button className="ghost sm-btn" onClick={() => go(`/experiments?project=${pid}&algorithm=${a.id}&suite=${s}`)}>최적화 →</button></td></tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}

export function ProjectDetail({ id }: { id: string }) {
  const p = useAsync(() => api.project(id), [id], 8000);
  const m = useAsync(() => api.manifest(id).catch(() => null), [id, p.data?.latest_sha]);
  const revs = useAsync(() => api.revisions(id), [id, p.data?.latest_sha]);
  const benches = useAsync(() => api.benchmarks(id), [id], 8000);
  const [benching, setBenching] = useState(false);
  const [ref, setRef] = useState("");
  if (!p.data) return <div className="page"><Empty>{p.error || "불러오는 중…"}</Empty></div>;
  const proj = p.data;
  return (
    <div className="page">
      <Card title={<><a onClick={() => go("/projects")}>프로젝트</a> / {proj.name}</>}
        actions={<>
          <input className="inline" value={ref} placeholder={proj.default_ref || "기본 브랜치"} onChange={(e) => setRef(e.target.value)} />
          <button className="ghost" onClick={async () => { if (await act(() => api.sync(id, ref || undefined), "동기화했습니다")) { p.reload(); m.reload(); revs.reload(); } }}>⟳ 동기화</button>
          <button onClick={() => setBenching((v) => !v)}>▶ 벤치마크</button>
        </>}>
        <div className="row gap wrap sm">
          <span className="muted">{proj.repo_kind === "git" ? proj.repo_url : "로컬 경로 (Runner 측)"}</span>
          <span>최신 <Sha sha={proj.latest_sha} /> <span className="muted">{ago(proj.synced_at)}</span></span>
          <label className="check"><input type="checkbox" checked={proj.watch} disabled={proj.repo_kind !== "git"}
            onChange={async (e) => { if (await act(() => api.updateProject(id, { watch: e.target.checked }))) p.reload(); }} /> 새 커밋 감지</label>
          {proj.slave_online ? <span className="pill ok">● Runner 연결</span> : <span className="pill bad">○ 연결된 Runner 없음</span>}
        </div>
        {benching && <div style={{ marginTop: 12 }}><NewBenchmark projects={[proj]} preset={proj.project_id} onCreated={(b) => go(`/benchmarks/${b.benchmark_id}`)} /></div>}
      </Card>

      <Card title="알고리즘">
        {!m.data && <Empty>아직 동기화되지 않았습니다. ⟳ 동기화를 눌러 매니페스트를 읽어 오세요.</Empty>}
        {m.data && !m.data.valid && <div className="bad">매니페스트가 유효하지 않습니다.</div>}
        {m.data?.algorithms?.map((a) => <AlgorithmCard key={a.id} pid={id} a={a} />)}
      </Card>

      <div className="two-col">
        <Card title="최근 벤치마크">
          {!benches.data?.length && <Empty>없음</Empty>}
          {!!benches.data?.length && (
            <table className="list compact"><tbody>{benches.data.slice(0, 10).map((b) => (
              <tr key={b.benchmark_id} className="click" onClick={() => go(`/benchmarks/${b.benchmark_id}`)}>
                <td>{b.name}</td><td><StatusPill s={b.status} /></td><td><SummaryChips summary={b.summary} /></td><td className="muted">{ago(b.created_at)}</td>
              </tr>
            ))}</tbody></table>
          )}
        </Card>
        <Card title="리비전">
          {!revs.data?.length && <Empty>없음</Empty>}
          {!!revs.data?.length && (
            <table className="list compact"><tbody>{revs.data.map((r) => (
              <tr key={r.revision_id}><td><Sha sha={r.sha} /></td><td className="muted">{r.ref || "기본"}</td><td className="muted">{ago(r.synced_at)}</td></tr>
            ))}</tbody></table>
          )}
        </Card>
      </div>
    </div>
  );
}
