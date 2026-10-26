import { useEffect, useMemo, useState } from "react";
import { api, openWs, type Benchmark, type Cell, type Project } from "../api";
import { act, ago, Card, Empty, Field, fmt, Modal, opText, Sha, StatusPill, TextArtifact, useAsync, VerdictBadge } from "../ui";
import { go } from "../nav";

/** The metric a cell is "about": regression-tracked first, then gates, then anything reported. */
export function headlineMetric(c: Cell): string | null {
  const d = Object.keys(c.deltas || {});
  if (d.length) return d[0];
  if (c.gates?.length) return c.gates[0].metric;
  const m = Object.keys(c.metrics || {});
  return m.length ? m[0] : null;
}

export function SummaryChips({ summary }: { summary: Record<string, number> }) {
  const order = ["passed", "improved", "regressed", "gate_failed", "error"];
  const items = order.filter((k) => summary?.[k]);
  if (!items.length) return <span className="muted sm">–</span>;
  return <span className="row gap-s">{items.map((k) => <span key={k} className="row gap-xs"><VerdictBadge v={k} compact />{summary[k]}</span>)}</span>;
}

/** project → algorithm rows × suite columns. */
export function Matrix({ cells, onPick, compact }: { cells: Cell[]; onPick?: (c: Cell) => void; compact?: boolean }) {
  const groups = useMemo(() => {
    const byProject = new Map<string, Cell[]>();
    cells.forEach((c) => byProject.set(c.project_id, [...(byProject.get(c.project_id) || []), c]));
    return [...byProject.entries()].map(([pid, cs]) => {
      const suites = [...new Set(cs.map((c) => c.suite_id))];
      const algos = [...new Set(cs.map((c) => c.algorithm_id))];
      return { pid, name: cs[0].project_name || pid.slice(0, 8), sha: cs.find((c) => c.commit_sha)?.commit_sha, suites, algos, cs };
    });
  }, [cells]);
  if (!cells.length) return <Empty>셀이 없습니다.</Empty>;
  return (
    <div className="matrix-wrap">
      {groups.map((g) => (
        <div key={g.pid} className="matrix-group">
          <div className="row gap-s matrix-title">
            <b>{g.name}</b><Sha sha={g.sha} />
          </div>
          <div className="scroll-x">
            <table className="matrix">
              <thead>
                <tr><th>알고리즘 \ 스위트</th>{g.suites.map((s) => <th key={s}>{s}</th>)}</tr>
              </thead>
              <tbody>
                {g.algos.map((a) => (
                  <tr key={a}>
                    <th scope="row">{a}</th>
                    {g.suites.map((s) => {
                      const c = g.cs.find((x) => x.algorithm_id === a && x.suite_id === s);
                      if (!c) return <td key={s} className="na">·</td>;
                      const m = headlineMetric(c);
                      const d = m ? c.deltas?.[m] : undefined;
                      return (
                        <td key={s} className={`cell v-${c.verdict}`} onClick={() => onPick?.(c)} role={onPick ? "button" : undefined} tabIndex={onPick ? 0 : undefined}
                          onKeyDown={(e) => { if (e.key === "Enter") onPick?.(c); }}>
                          <VerdictBadge v={c.verdict} compact={compact} />
                          {!compact && m && c.metrics && m in c.metrics && (
                            <div className="cell-metric"><span className="muted">{m}</span> {fmt(c.metrics[m], 3)}
                              {d && d.delta !== null && d.status !== "same" && (
                                <span className={`delta ${d.status}`}>{d.delta > 0 ? "+" : ""}{fmt(d.delta, 2)}</span>
                              )}
                            </div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

function CellDetail({ c, onClose, onAccepted }: { c: Cell; onClose: () => void; onAccepted: () => void }) {
  const [tab, setTab] = useState<"result" | "summary" | "log">("result");
  const [logUri, setLogUri] = useState<string | null>(null);
  const [summaryUri, setSummaryUri] = useState<string | null>(null);
  useEffect(() => {
    if (!c.run_id) return;
    fetch(`/api/v1/runs/${c.run_id}`).then((r) => r.json()).then((r) => { setLogUri(r.log_uri); setSummaryUri(r.summary_md_uri); }).catch(() => {});
  }, [c.run_id]);
  return (
    <Modal title={`${c.project_name} · ${c.algorithm_id} @ ${c.suite_id}`} onClose={onClose}>
      <div className="row gap wrap" style={{ marginBottom: 10 }}>
        <VerdictBadge v={c.verdict} />
        <span className="muted sm">커밋 <Sha sha={c.commit_sha} /></span>
        {c.baseline_sha && <span className="muted sm">베이스라인 <Sha sha={c.baseline_sha} /></span>}
        {c.duration_s != null && <span className="muted sm">{fmt(c.duration_s, 3)}s</span>}
        <span style={{ flex: 1 }} />
        {c.metrics && <button className="ghost" onClick={async () => { if (await act(() => api.accept(c.benchmark_id, c.cell_id), "새 베이스라인으로 승인했습니다")) onAccepted(); }}>베이스라인으로 승인</button>}
      </div>
      <div className="tabs">
        <button className={tab === "result" ? "tab on" : "tab"} onClick={() => setTab("result")}>판정</button>
        <button className={tab === "summary" ? "tab on" : "tab"} disabled={!summaryUri} onClick={() => setTab("summary")}>리포트</button>
        <button className={tab === "log" ? "tab on" : "tab"} disabled={!logUri} onClick={() => setTab("log")}>로그</button>
      </div>
      {tab === "result" && (
        <>
          {c.error && <pre className="artifact bad">{c.error}</pre>}
          {c.gates.length > 0 && (
            <>
              <h3>게이트</h3>
              <table><thead><tr><th>조건</th><th>실제</th><th>결과</th></tr></thead>
                <tbody>{c.gates.map((g) => (
                  <tr key={g.metric + g.op}><td><code>{g.metric} {opText(g.op)} {g.value}</code></td><td>{fmt(g.actual)}</td><td>{g.passed ? <span className="pill ok">✓</span> : <span className="pill bad">✕</span>}</td></tr>
                ))}</tbody></table>
            </>
          )}
          {Object.keys(c.deltas).length > 0 && (
            <>
              <h3>베이스라인 대비</h3>
              <table><thead><tr><th>지표</th><th>베이스라인</th><th>현재</th><th>Δ</th><th>허용폭</th><th></th></tr></thead>
                <tbody>{Object.entries(c.deltas).map(([m, d]) => (
                  <tr key={m}><td><code>{m}</code> <span className="muted sm">{d.direction === "minimize" ? "↓" : "↑"}</span></td>
                    <td>{fmt(d.baseline)}</td><td>{fmt(d.current)}</td><td>{fmt(d.delta, 3)}</td><td>{fmt(d.tolerance, 2)}</td>
                    <td><span className={`pill ${d.status === "regressed" ? "warn" : d.status === "improved" ? "acc" : ""}`}>{d.status}</span></td></tr>
                ))}</tbody></table>
            </>
          )}
          {c.metrics && (
            <>
              <h3>지표</h3>
              <div className="kv-grid">{Object.entries(c.metrics).map(([k, v]) => <div key={k} className="kv"><code>{k}</code><b>{fmt(v)}</b></div>)}</div>
            </>
          )}
        </>
      )}
      {tab === "summary" && summaryUri && <TextArtifact uri={summaryUri} />}
      {tab === "log" && logUri && <TextArtifact uri={logUri} />}
    </Modal>
  );
}

export function NewBenchmark({ projects, onCreated, preset }: { projects: Project[]; onCreated: (b: Benchmark) => void; preset?: string }) {
  const [sel, setSel] = useState<string[]>(preset ? [preset] : []);
  const [tags, setTags] = useState("");
  const [ref, setRef] = useState("");
  const [name, setName] = useState("수동 벤치마크");
  const submit = async () => {
    const tagList = tags.split(",").map((t) => t.trim()).filter(Boolean);
    const scope = sel.length
      ? sel.map((pid) => ({ project_id: pid, ...(ref ? { ref } : {}), ...(tagList.length ? { tags: tagList } : {}) }))
      : null;
    const b = await act(() => api.createBenchmark({ name, scope }), "벤치마크를 시작했습니다");
    if (b) onCreated(b);
  };
  return (
    <div className="form-grid">
      <Field label="이름"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
      <Field label="프로젝트" hint="비우면 전체">
        <div className="chips">
          {projects.map((p) => (
            <button key={p.project_id} className={sel.includes(p.project_id) ? "chip on" : "chip"}
              onClick={() => setSel((s) => (s.includes(p.project_id) ? s.filter((x) => x !== p.project_id) : [...s, p.project_id]))}>{p.name}</button>
          ))}
        </div>
      </Field>
      <Field label="ref" hint="선택한 프로젝트에 적용 (브랜치/태그/sha)"><input value={ref} placeholder="기본 브랜치" disabled={!sel.length} onChange={(e) => setRef(e.target.value)} /></Field>
      <Field label="스위트 태그" hint="쉼표 구분"><input value={tags} placeholder="예: fast" disabled={!sel.length} onChange={(e) => setTags(e.target.value)} /></Field>
      <div><button onClick={submit}>▶ 벤치마크 실행</button></div>
    </div>
  );
}

export function BenchmarksView() {
  const list = useAsync(() => api.benchmarks(), [], 5000);
  const projects = useAsync(() => api.projects(), []);
  const [creating, setCreating] = useState(false);
  return (
    <div className="page">
      <Card title="벤치마크" actions={<button onClick={() => setCreating((v) => !v)}>{creating ? "닫기" : "+ 새 벤치마크"}</button>}>
        {creating && <NewBenchmark projects={projects.data || []} onCreated={(b) => { setCreating(false); go(`/benchmarks/${b.benchmark_id}`); }} />}
        {list.error && <div className="bad sm">{list.error}</div>}
        {list.data && list.data.length === 0 && <Empty>아직 벤치마크가 없습니다. 프로젝트를 등록하고 실행해 보세요.</Empty>}
        {list.data && list.data.length > 0 && (
          <table className="list">
            <thead><tr><th>이름</th><th>상태</th><th>트리거</th><th>결과</th><th>시작</th></tr></thead>
            <tbody>{list.data.map((b) => (
              <tr key={b.benchmark_id} className="click" onClick={() => go(`/benchmarks/${b.benchmark_id}`)}>
                <td>{b.name}</td><td><StatusPill s={b.status} /></td><td className="muted">{b.trigger}</td>
                <td><SummaryChips summary={b.summary} /></td><td className="muted">{ago(b.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

export function BenchmarkDetail({ id }: { id: string }) {
  const b = useAsync(() => api.benchmark(id), [id]);
  const [pick, setPick] = useState<Cell | null>(null);
  useEffect(() => {
    const ws = openWs("benchmarks", id, (m) => {
      if (["cell_done", "benchmark_done", "benchmark_planned"].includes(m.type)) b.reload();
    });
    const t = setInterval(() => { if (b.data && ["created", "running"].includes(b.data.status)) b.reload(); }, 4000);
    return () => { ws.close(); clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, b.data?.status]);
  if (!b.data) return <div className="page"><Empty>{b.error || "불러오는 중…"}</Empty></div>;
  const x = b.data;
  const failing = (x.cells || []).filter((c) => ["regressed", "gate_failed", "error"].includes(c.verdict));
  return (
    <div className="page">
      <Card title={<><a onClick={() => go("/benchmarks")}>벤치마크</a> / {x.name}</>}
        actions={<>
          <StatusPill s={x.status} />
          {failing.length > 0 && x.status !== "running" && (
            <button className="ghost" onClick={async () => { if (await act(() => api.accept(x.benchmark_id), "모든 셀을 새 베이스라인으로 승인했습니다")) b.reload(); }}>전체를 베이스라인으로 승인</button>
          )}
        </>}>
        <div className="row gap wrap muted sm">
          <span>트리거 {x.trigger}</span><span>시작 {ago(x.created_at)}</span>
          {x.finished_at && <span>종료 {ago(x.finished_at)}</span>}
          <SummaryChips summary={x.summary} />
        </div>
      </Card>
      <Card title="매트릭스" actions={<span className="muted sm">셀을 누르면 게이트·베이스라인 비교·로그를 볼 수 있습니다</span>}>
        <Matrix cells={x.cells || []} onPick={setPick} />
      </Card>
      {failing.length > 0 && (
        <Card title={`실패 ${failing.length}건`}>
          <ul className="fail-list">
            {failing.map((c) => (
              <li key={c.cell_id} className="click" onClick={() => setPick(c)}>
                <VerdictBadge v={c.verdict} /> <b>{c.project_name}</b> · {c.algorithm_id} @ {c.suite_id}
                <span className="muted sm"> — {c.error?.split("\n")[0]
                  || c.gates.filter((g) => !g.passed).map((g) => `${g.metric} ${opText(g.op)} ${g.value} (실제 ${fmt(g.actual, 3)})`).join(", ")
                  || Object.entries(c.deltas).filter(([, d]) => d.status === "regressed").map(([m, d]) => `${m} ${fmt(d.baseline, 3)} → ${fmt(d.current, 3)}`).join(", ")}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      {pick && <CellDetail c={pick} onClose={() => setPick(null)} onAccepted={() => { setPick(null); b.reload(); }} />}
    </div>
  );
}
