import { useState } from "react";
import { api, type BenchmarkScope } from "../api";
import { go } from "../nav";
import { act, ago, Card, Empty, Field, Sha, StatusPill, Tiles, useAsync } from "../ui";
import { Matrix, SummaryChips } from "./benchmarks";

export function OverviewView() {
  const projects = useAsync(() => api.projects(), [], 8000);
  const runners = useAsync(() => api.runners(), [], 8000);
  const benches = useAsync(() => api.benchmarks(), [], 6000);
  const exps = useAsync(() => api.experiments(), [], 6000);
  const latestId = benches.data?.find((b) => b.status !== "created")?.benchmark_id;
  const latest = useAsync(() => (latestId ? api.benchmark(latestId) : Promise.resolve(null)), [latestId, benches.data?.[0]?.status], 6000);
  const online = runners.data?.filter((r) => r.online) || [];
  const slots = online.reduce((a, r) => a + r.concurrency, 0);
  const busy = online.reduce((a, r) => a + r.inflight, 0);
  const running = (exps.data || []).filter((e) => ["running", "paused"].includes(e.status));
  const lb = latest.data;
  const tone = lb?.status === "passed" ? "ok" : lb?.status === "failed" || lb?.status === "error" ? "bad" : "";
  return (
    <div className="page">
      <Tiles items={[
        { label: "프로젝트", value: projects.data?.length ?? "–" },
        { label: "Runner", value: <>{online.length}<span className="muted sm"> / {runners.data?.length ?? 0} 온라인</span></> , tone: online.length ? "" : "bad" },
        { label: "실행 슬롯", value: <>{busy}<span className="muted sm"> / {slots} 사용 중</span></> },
        { label: "최근 벤치마크", value: lb ? <StatusPill s={lb.status} /> : "–", tone },
        { label: "진행 중 캠페인", value: running.length },
      ]} />
      {!projects.data?.length && (
        <Card title="시작하기">
          <ol className="steps">
            <li>Runner를 띄웁니다: <code>ALGOPS_SLAVE_MASTER_URL=http://&lt;master&gt;:8000 algops slave</code> (여러 대 가능, 아웃바운드만 필요)</li>
            <li>알고리즘 레포에 <code>algops init</code> → <code>algops test</code> 로 <code>algops.yaml</code>을 만듭니다 (어떤 언어든 가능).</li>
            <li><a onClick={() => go("/projects")}>프로젝트</a>에서 Git URL을 등록하면 모든 알고리즘 × 스위트를 벤치마크할 수 있습니다.</li>
          </ol>
        </Card>
      )}
      {lb && (
        <Card title={<>최근 벤치마크 — <a onClick={() => go(`/benchmarks/${lb.benchmark_id}`)}>{lb.name}</a></>}
          actions={<><SummaryChips summary={lb.summary} /><span className="muted sm">{ago(lb.created_at)}</span></>}>
          <Matrix cells={lb.cells || []} onPick={() => go(`/benchmarks/${lb.benchmark_id}`)} />
        </Card>
      )}
      {running.length > 0 && (
        <Card title="진행 중 캠페인">
          <table className="list compact"><tbody>{running.map((e) => (
            <tr key={e.experiment_id} className="click" onClick={() => go(`/experiments/${e.experiment_id}`)}>
              <td>{e.name}</td><td>{e.algorithm_id} @ {e.suite_id}</td><td><StatusPill s={e.status} /></td><td><Sha sha={e.commit_sha} /></td>
            </tr>
          ))}</tbody></table>
        </Card>
      )}
    </div>
  );
}

export function RunnersView() {
  const runners = useAsync(() => api.runners(), [], 4000);
  return (
    <div className="page">
      <Card title="Runner (슬레이브)" actions={<span className="muted sm">아웃바운드 전용 · 여러 레포를 병렬 실행</span>}>
        {runners.data?.length === 0 && <Empty>연결된 Runner가 없습니다. <code>algops slave</code> 로 실행하세요.</Empty>}
        {!!runners.data?.length && (
          <table className="list">
            <thead><tr><th>호스트</th><th>상태</th><th>라벨</th><th>동시 실행</th><th>Git</th><th>로컬 프로젝트</th><th>허용 프로젝트</th><th>하트비트</th><th>버전</th></tr></thead>
            <tbody>{runners.data.map((r) => (
              <tr key={r.slave_id} className={r.online ? "" : "dim"}>
                <td><b>{r.host_label}</b> <span className="muted sm">{r.slave_id.slice(0, 8)}</span></td>
                <td>{r.online ? <span className={`pill ${r.inflight ? "acc" : "ok"}`}>{r.inflight ? "작업 중" : "대기"}</span> : <span className="pill">오프라인</span>}</td>
                <td>{r.labels.length ? r.labels.map((l) => <span key={l} className="tag">{l}</span>) : <span className="muted">–</span>}</td>
                <td>{r.inflight} / {r.concurrency}</td>
                <td>{r.git ? "✓" : "–"}</td>
                <td className="sm">{r.local_projects.join(", ") || "–"}</td>
                <td className="sm">{r.allow_projects ? r.allow_projects.join(", ") : "전체"}</td>
                <td className="muted">{ago(r.last_heartbeat)}</td>
                <td className="muted sm">{r.version}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

export function SchedulesView() {
  const list = useAsync(() => api.schedules(), [], 8000);
  const projects = useAsync(() => api.projects(), []);
  const [f, setF] = useState({ name: "nightly", kind: "cron" as "cron" | "watch", cron: "0 2 * * *", interval_s: 300, projects: [] as string[] });
  const names = Object.fromEntries((projects.data || []).map((p) => [p.project_id, p.name]));
  const create = async () => {
    const scope: BenchmarkScope[] | null = f.projects.length ? f.projects.map((project_id) => ({ project_id })) : null;
    if (await act(() => api.createSchedule({ name: f.name, kind: f.kind, cron: f.kind === "cron" ? f.cron : null, interval_s: Number(f.interval_s), scope }), "스케줄을 만들었습니다")) list.reload();
  };
  return (
    <div className="page">
      <Card title="스케줄 · 트리거">
        <div className="form-grid">
          <Field label="이름"><input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label="종류">
            <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value as "cron" | "watch" })}>
              <option value="cron">cron — 정해진 시각에 벤치마크</option><option value="watch">watch — 새 커밋이 오면 벤치마크</option>
            </select>
          </Field>
          {f.kind === "cron"
            ? <Field label="cron" hint="서버 로컬 시간, 예: 0 2 * * * (매일 02:00)"><input value={f.cron} onChange={(e) => setF({ ...f, cron: e.target.value })} /></Field>
            : <Field label="확인 주기 (초)"><input type="number" min={30} value={f.interval_s} onChange={(e) => setF({ ...f, interval_s: Number(e.target.value) })} /></Field>}
          <Field label="프로젝트" hint="비우면 전체">
            <div className="chips">{projects.data?.map((p) => (
              <button key={p.project_id} className={f.projects.includes(p.project_id) ? "chip on" : "chip"}
                onClick={() => setF((x) => ({ ...x, projects: x.projects.includes(p.project_id) ? x.projects.filter((y) => y !== p.project_id) : [...x.projects, p.project_id] }))}>{p.name}</button>
            ))}</div>
          </Field>
          <div><button onClick={create}>+ 스케줄 추가</button></div>
          <p className="muted sm span2">CI에서는 <code>algops ci --master URL --project NAME --ref $SHA</code> 로 벤치마크를 걸고 결과로 빌드를 막을 수 있습니다. 레포 안에서는 <code>algops test</code> 만으로도 게이트를 확인합니다.</p>
        </div>
      </Card>
      <Card title="등록된 스케줄">
        {list.data?.length === 0 && <Empty>없음</Empty>}
        {!!list.data?.length && (
          <table className="list">
            <thead><tr><th>이름</th><th>종류</th><th>범위</th><th>마지막 실행</th><th>다음 실행</th><th>최근 벤치마크</th><th></th></tr></thead>
            <tbody>{list.data.map((s) => (
              <tr key={s.schedule_id} className={s.enabled ? "" : "dim"}>
                <td><b>{s.name}</b></td>
                <td>{s.kind === "cron" ? <code>{s.cron}</code> : `watch · ${s.interval_s}s`}</td>
                <td className="sm">{s.scope ? s.scope.map((x) => names[x.project_id] || x.project_id.slice(0, 8)).join(", ") : "전체"}</td>
                <td className="muted">{ago(s.last_fired_at)}</td>
                <td className="muted">{s.next_fire_at ? new Date(s.next_fire_at).toLocaleString("ko-KR") : "–"}</td>
                <td>{s.last_benchmark_id ? <a onClick={() => go(`/benchmarks/${s.last_benchmark_id}`)}>보기</a> : "–"}</td>
                <td className="row gap-s">
                  <button className="ghost sm-btn" onClick={async () => { const r = await act(() => api.fireSchedule(s.schedule_id)); if (r?.benchmark_id) go(`/benchmarks/${r.benchmark_id}`); else list.reload(); }}>지금 실행</button>
                  <button className="ghost sm-btn" onClick={async () => { if (await act(() => api.toggleSchedule(s.schedule_id, !s.enabled))) list.reload(); }}>{s.enabled ? "끄기" : "켜기"}</button>
                  <button className="ghost sm-btn" onClick={async () => { if (confirm(`'${s.name}' 스케줄을 삭제할까요?`) && await act(() => api.deleteSchedule(s.schedule_id))) list.reload(); }}>삭제</button>
                </td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
