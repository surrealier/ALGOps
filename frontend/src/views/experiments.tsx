import { useEffect, useMemo, useState } from "react";
import { api, openWs, type Constraint, type Experiment, type FinalResult, type ManifestOut, type Objective, type ParamDef, type Run } from "../api";
import { go } from "../nav";
import { act, ago, Card, Empty, Field, fmt, opText, ProgressChart, Sha, StatusPill, TextArtifact, useAsync } from "../ui";

const TERMINAL = ["target_reached", "stopped_max_iter", "stopped_by_pm", "failed"];
const OPS: Constraint["op"][] = [">=", "<=", ">", "<"];

function objText(o: Objective | null, fallback?: Experiment) {
  if (!o && fallback) return `${fallback.target_metric} ${fallback.goal_direction}`;
  if (!o) return "–";
  let t = `${o.metric} ${o.direction === "minimize" ? "↓" : "↑"}`;
  if (o.target != null) t += ` (${o.direction === "minimize" ? "≤" : "≥"} ${o.target})`;
  if (o.constraints.length) t += " · " + o.constraints.map((c) => `${c.metric} ${opText(c.op)} ${c.value}`).join(", ");
  return t;
}

const feasible = (o: Objective | null, m: Record<string, number> | null) =>
  !!m && (o?.constraints || []).every((c) => {
    const v = m[c.metric];
    if (v === undefined) return false;
    return c.op === ">=" ? v >= c.value : c.op === "<=" ? v <= c.value : c.op === ">" ? v > c.value : c.op === "<" ? v < c.value : v === c.value;
  });

// ------------------------------------------------------------------------------- create

function NewExperiment({ initial, onCreated }: { initial: { project?: string; algorithm?: string; suite?: string }; onCreated: (e: Experiment) => void }) {
  const projects = useAsync(() => api.projects(), []);
  const [pid, setPid] = useState(initial.project || "");
  const manifest = useAsync<ManifestOut | null>(() => (pid ? api.manifest(pid).catch(() => null) : Promise.resolve(null)), [pid]);
  const [algo, setAlgo] = useState(initial.algorithm || "");
  const info = manifest.data?.algorithms?.find((a) => a.id === algo) || manifest.data?.algorithms?.[0];
  const params: ParamDef[] = useMemo(() => manifest.data?.manifest.algorithms.find((a) => a.id === info?.id)?.param_spec?.params || [], [manifest.data, info?.id]);
  const [suite, setSuite] = useState(initial.suite || "");
  const [validation, setValidation] = useState<string[]>([]);
  const [obj, setObj] = useState<Objective | null>(null);
  const [keys, setKeys] = useState<string[] | null>(null);
  const [f, setF] = useState({ name: "", goal_text: "", strategy: "coordinate", batch_size: 2, max_iteration: 12, ref: "" });

  useEffect(() => {
    if (!projects.data?.length) return;
    const hit = projects.data.find((p) => p.project_id === pid || p.name === pid);
    if (!hit) setPid(projects.data[0].project_id);
    else if (hit.project_id !== pid) setPid(hit.project_id);  // a name from a link -> its id
  }, [projects.data, pid]);
  useEffect(() => {
    if (!info) return;
    if (!algo) setAlgo(info.id);
    setSuite((s) => (s && info.suites.includes(s) ? s : info.suites[0]));
    setObj(info.objective ? { ...info.objective, constraints: [...info.objective.constraints] } : null);
    setKeys(null);
    setValidation([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info?.id]);

  const metrics = info ? Object.keys(info.metrics) : [];
  const setMetric = (metric: string) => setObj((o) => ({ metric, direction: info?.metrics[metric] || o?.direction || "maximize", target: o?.target ?? null, constraints: o?.constraints || [] }));
  const submit = async () => {
    if (!pid || !info) return;
    const body: Record<string, unknown> = {
      project_id: pid, algorithm_id: info.id, suite_id: suite, validation_suite_ids: validation,
      name: f.name || `${info.id} 최적화 ${new Date().toLocaleString("ko-KR")}`, goal_text: f.goal_text,
      strategy: f.strategy, batch_size: Number(f.batch_size), max_iteration: Number(f.max_iteration),
      ref: f.ref || null, search_keys: keys,
    };
    if (obj) body.objective = obj;
    const e = await act(() => api.createExperiment(body), "캠페인을 만들었습니다");
    if (e) onCreated(e);
  };

  return (
    <div className="form-grid">
      <Field label="프로젝트">
        <select value={pid} onChange={(e) => { setPid(e.target.value); setAlgo(""); }}>
          {projects.data?.map((p) => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
        </select>
      </Field>
      <Field label="알고리즘">
        <select value={info?.id || ""} onChange={(e) => setAlgo(e.target.value)} disabled={!manifest.data}>
          {manifest.data?.algorithms?.map((a) => <option key={a.id} value={a.id}>{a.id} ({a.params} params)</option>)}
        </select>
      </Field>
      {!manifest.data && pid && <p className="muted sm span2">프로젝트를 먼저 동기화하세요 (프로젝트 화면 ⟳).</p>}
      {info && (
        <>
          <Field label="튜닝 스위트">
            <select value={suite} onChange={(e) => setSuite(e.target.value)}>{info.suites.map((s) => <option key={s}>{s}</option>)}</select>
          </Field>
          <Field label="검증 스위트" hint="최적값을 held-out으로 재확인">
            <div className="chips">{info.suites.filter((s) => s !== suite).map((s) => (
              <button key={s} className={validation.includes(s) ? "chip on" : "chip"} onClick={() => setValidation((v) => (v.includes(s) ? v.filter((x) => x !== s) : [...v, s]))}>{s}</button>
            ))}{info.suites.length < 2 && <span className="muted sm">다른 스위트 없음</span>}</div>
          </Field>
          <Field label="목표 지표">
            <div className="row gap-s">
              <select value={obj?.metric || ""} onChange={(e) => setMetric(e.target.value)}>
                {metrics.map((m) => <option key={m} value={m}>{m} {info.metrics[m] === "minimize" ? "↓" : "↑"}</option>)}
              </select>
              <select value={obj?.direction || "maximize"} onChange={(e) => setObj((o) => (o ? { ...o, direction: e.target.value as Objective["direction"] } : o))}>
                <option value="minimize">최소화</option><option value="maximize">최대화</option>
              </select>
              <input type="number" step="any" placeholder="목표값 (선택)" value={obj?.target ?? ""}
                onChange={(e) => setObj((o) => (o ? { ...o, target: e.target.value === "" ? null : Number(e.target.value) } : o))} />
            </div>
          </Field>
          <Field label="제약" hint="만족하는 후보가 항상 우선">
            <div className="stack">
              {(obj?.constraints || []).map((c, i) => (
                <div key={i} className="row gap-s">
                  <select value={c.metric} onChange={(e) => setObj((o) => o && { ...o, constraints: o.constraints.map((x, j) => (j === i ? { ...x, metric: e.target.value } : x)) })}>
                    {metrics.map((m) => <option key={m}>{m}</option>)}
                  </select>
                  <select value={c.op} onChange={(e) => setObj((o) => o && { ...o, constraints: o.constraints.map((x, j) => (j === i ? { ...x, op: e.target.value as Constraint["op"] } : x)) })}>
                    {OPS.map((op) => <option key={op} value={op}>{opText(op)}</option>)}
                  </select>
                  <input type="number" step="any" value={c.value} onChange={(e) => setObj((o) => o && { ...o, constraints: o.constraints.map((x, j) => (j === i ? { ...x, value: Number(e.target.value) } : x)) })} />
                  <button className="ghost" aria-label="제약 삭제" onClick={() => setObj((o) => o && { ...o, constraints: o.constraints.filter((_, j) => j !== i) })}>✕</button>
                </div>
              ))}
              <button className="ghost sm-btn" disabled={!obj} onClick={() => setObj((o) => o && { ...o, constraints: [...o.constraints, { metric: metrics[0], op: ">=", value: 0 }] })}>+ 제약 추가</button>
            </div>
          </Field>
          <Field label="탐색 파라미터" hint="비우면 전체">
            <div className="chips">{params.map((p) => {
              const on = keys === null || keys.includes(p.key);
              return <button key={p.key} className={on ? "chip on" : "chip"} title={p.description || ""}
                onClick={() => setKeys((k) => { const cur = k ?? params.map((x) => x.key); return cur.includes(p.key) ? cur.filter((x) => x !== p.key) : [...cur, p.key]; })}>{p.key}</button>;
            })}</div>
          </Field>
          <Field label="전략">
            <select value={f.strategy} onChange={(e) => setF({ ...f, strategy: e.target.value })}>
              <option value="coordinate">coordinate (패턴 탐색)</option><option value="random">random</option><option value="grid">grid (전수)</option>
            </select>
          </Field>
          <Field label="배치 / 최대 반복" hint="배치 = 반복당 병렬 후보 수">
            <div className="row gap-s">
              <input type="number" min={1} max={64} value={f.batch_size} onChange={(e) => setF({ ...f, batch_size: Number(e.target.value) })} />
              <input type="number" min={1} max={500} value={f.max_iteration} onChange={(e) => setF({ ...f, max_iteration: Number(e.target.value) })} />
            </div>
          </Field>
          <Field label="ref" hint="비우면 기본 브랜치"><input value={f.ref} onChange={(e) => setF({ ...f, ref: e.target.value })} /></Field>
          <Field label="목표 메모 (자연어)"><input value={f.goal_text} placeholder="예: 오탐 완화, recall 유지" onChange={(e) => setF({ ...f, goal_text: e.target.value })} /></Field>
          <div><button onClick={submit}>캠페인 생성</button></div>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------- list

export function ExperimentsView({ query }: { query: URLSearchParams }) {
  const list = useAsync(() => api.experiments(), [], 5000);
  const projects = useAsync(() => api.projects(), []);
  const [creating, setCreating] = useState(query.has("project"));
  const names = Object.fromEntries((projects.data || []).map((p) => [p.project_id, p.name]));
  return (
    <div className="page">
      <Card title="최적화 캠페인" actions={<button onClick={() => setCreating((v) => !v)}>{creating ? "닫기" : "+ 새 캠페인"}</button>}>
        {creating && <NewExperiment initial={{ project: query.get("project") || undefined, algorithm: query.get("algorithm") || undefined, suite: query.get("suite") || undefined }}
          onCreated={(e) => go(`/experiments/${e.experiment_id}`)} />}
        {list.data?.length === 0 && <Empty>캠페인이 없습니다.</Empty>}
        {!!list.data?.length && (
          <table className="list">
            <thead><tr><th>이름</th><th>프로젝트</th><th>알고리즘 @ 스위트</th><th>목표</th><th>상태</th><th>생성</th></tr></thead>
            <tbody>{list.data.map((e) => (
              <tr key={e.experiment_id} className="click" onClick={() => go(`/experiments/${e.experiment_id}`)}>
                <td>{e.name}</td><td>{names[e.project_id] || "…"}</td><td>{e.algorithm_id} @ {e.suite_id}</td>
                <td className="sm">{objText(e.objective, e)}</td><td><StatusPill s={e.status} /></td><td className="muted">{ago(e.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

// ------------------------------------------------------------------------------- detail

function Promotion({ e, final, reload }: { e: Experiment; final: FinalResult; reload: () => void }) {
  const p = final.promotion;
  const changed = Object.entries(final.changed_params || {});
  const heldOutFail = Object.entries(final.validation || {}).filter(([, v]) => v.best && (!v.best.feasible || v.improved === false)).map(([s]) => s);
  const [busy, setBusy] = useState(false);
  const promote = async (pull_request: boolean) => {
    setBusy(true);
    await act(() => api.promote(e.experiment_id, { push: true, pull_request }), pull_request ? "PR을 요청했습니다" : "브랜치에 커밋했습니다");
    setBusy(false);
    reload();
  };
  return (
    <Card title="Git으로 제안 (승격)">
      {changed.length === 0 && <Empty>최적값이 현재 기본값과 같습니다 — 제안할 변경이 없습니다.</Empty>}
      {changed.length > 0 && (
        <>
          <table className="compact"><thead><tr><th>파라미터</th><th>현재 기본값</th><th>제안</th></tr></thead>
            <tbody>{changed.map(([k, c]) => <tr key={k}><td><code>{k}</code></td><td>{fmt(c.old)}</td><td><b>{fmt(c.new)}</b></td></tr>)}</tbody></table>
          {heldOutFail.length > 0 && <p className="warn sm">⚠ 검증 스위트 {heldOutFail.join(", ")}에서 개선이 유지되지 않았습니다 — 승격 전에 확인하세요 (튜닝 스위트에 과적합되었을 수 있습니다).</p>}
          <p className="muted sm">기준 커밋 <Sha sha={e.commit_sha} /> 위에 새 브랜치로 커밋합니다. 기본 브랜치는 바뀌지 않으며, 사람이 리뷰 후 머지합니다.</p>
          <div className="row gap">
            <button disabled={busy} onClick={() => promote(false)}>브랜치로 커밋 + push</button>
            <button disabled={busy} className="ghost" onClick={() => promote(true)}>PR / MR 열기</button>
          </div>
        </>
      )}
      {p && (
        <div className="promo">
          {p.ok ? (
            <div className="row gap wrap sm">
              <span>브랜치 <code>{p.branch}</code></span><span>커밋 <Sha sha={p.commit_sha} /></span>
              <span>{p.pushed ? <span className="pill ok">push 완료</span> : <span className="pill warn">push 안 됨</span>}</span>
              {p.pr_url && <a href={p.pr_url} target="_blank" rel="noreferrer">PR 열기 ↗</a>}
              {p.patch_uri && <a href={api.artifactUrl(p.patch_uri)} target="_blank" rel="noreferrer">패치 보기</a>}
            </div>
          ) : <div className="bad sm">{p.error}</div>}
          {p.pr_error && <div className="warn sm">PR: {p.pr_error}</div>}
          {p.ok && p.error && <div className="warn sm">push: {p.error.split("\n")[0]}</div>}
        </div>
      )}
    </Card>
  );
}

export function ExperimentDetail({ id }: { id: string }) {
  const e = useAsync(() => api.experiment(id), [id]);
  const runs = useAsync(() => api.runs(id), [id]);
  const final = useAsync(() => api.final(id).catch(() => null), [id]);
  const iters = useAsync(() => fetch(`/api/v1/experiments/${id}/iterations`).then((r) => r.json()) as Promise<any[]>, [id]);
  const [approval, setApproval] = useState<any>(null);
  const [report, setReport] = useState(false);

  useEffect(() => {
    const ws = openWs("experiments", id, (m) => {
      if (["run_evaluated", "validation_evaluated", "iteration_decided"].includes(m.type)) { runs.reload(); iters.reload(); }
      if (m.type === "approval_required") { setApproval(m); e.reload(); }
      if (["experiment_started", "experiment_done", "experiment_failed", "finalized", "promoted"].includes(m.type)) { e.reload(); final.reload(); runs.reload(); }
    });
    const t = setInterval(() => { if (e.data && !TERMINAL.includes(e.data.status) && e.data.status !== "created") { e.reload(); runs.reload(); } }, 5000);
    return () => { ws.close(); clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, e.data?.status]);

  if (!e.data) return <div className="page"><Empty>{e.error || "불러오는 중…"}</Empty></div>;
  const x = e.data;
  const o: Objective | null = x.objective;
  const minimize = (o?.direction || x.goal_direction) === "minimize";
  const metric = o?.metric || x.target_metric;
  const search = (runs.data || []).filter((r) => r.kind === "search");
  const validation = (runs.data || []).filter((r) => r.kind === "validation");
  const keys = x.search_keys || Object.keys(search[0]?.param_values || {});
  const cons = o?.constraints || [];
  const points = search.map((r: Run) => ({
    x: r.iteration_index, y: r.metrics?.[metric] ?? null, ok: r.status === "evaluated",
    feasible: feasible(o, r.metrics), label: `iter ${r.iteration_index} · ${metric}=${fmt(r.metrics?.[metric])}\n${keys.map((k) => `${k}=${fmt(r.param_values[k])}`).join("\n")}`,
  }));
  const done = TERMINAL.includes(x.status);
  return (
    <div className="page">
      <Card title={<><a onClick={() => go("/experiments")}>캠페인</a> / {x.name}</>}
        actions={<>
          <StatusPill s={x.status} />
          <button disabled={x.status === "running" || x.status === "paused"} onClick={async () => { if (await act(() => api.run(id), "실행을 시작했습니다")) { e.reload(); runs.reload(); final.reload(); } }}>
            ▶ {x.attempt > 0 || done ? "다시 실행" : "실행"}
          </button>
        </>}>
        <div className="row gap wrap sm">
          <span><b>{x.algorithm_id}</b> @ {x.suite_id}</span>
          {x.validation_suite_ids.length > 0 && <span className="muted">검증: {x.validation_suite_ids.join(", ")}</span>}
          <span>목표 <code>{objText(o, x)}</code></span>
          <span className="muted">전략 {x.strategy} · 배치 {x.batch_size} · 최대 {x.max_iteration}회</span>
          <span>커밋 <Sha sha={x.commit_sha} /></span>
          <span className="muted">시도 #{x.attempt}</span>
        </div>
        {x.goal_text && <div className="muted sm" style={{ marginTop: 4 }}>“{x.goal_text}”</div>}
      </Card>

      {x.status === "paused" && (
        <Card title="승인 대기 (HITL)" className="warn-card">
          {approval?.proposed_batch ? (
            <table className="compact"><thead><tr>{keys.map((k) => <th key={k}>{k}</th>)}</tr></thead>
              <tbody>{approval.proposed_batch.map((p: any, i: number) => <tr key={i}>{keys.map((k) => <td key={k}>{fmt(p[k])}</td>)}</tr>)}</tbody></table>
          ) : <p className="muted sm">다음 후보를 평가하기 전에 승인이 필요합니다.</p>}
          {approval?.rationale && <p className="sm">근거: {approval.rationale}</p>}
          <div className="row gap">
            <button onClick={async () => { await act(() => api.approve(id, true)); setApproval(null); e.reload(); }}>승인</button>
            <button className="ghost" onClick={async () => { await act(() => api.approve(id, false)); setApproval(null); e.reload(); }}>거절 (중단)</button>
          </div>
        </Card>
      )}

      <Card title={`탐색 진행 — ${metric} ${minimize ? "↓" : "↑"}`} actions={<span className="muted sm">점 = 런 (빈 점 = 제약 위반) · 선 = 최선</span>}>
        <ProgressChart points={points} minimize={minimize} target={o?.target ?? x.target_value} />
      </Card>

      <Card title={`런 ${search.length}개`}>
        {search.length === 0 && <Empty>실행하면 반복마다 후보가 표시됩니다.</Empty>}
        {search.length > 0 && (
          <div className="scroll-x">
            <table className="list compact">
              <thead><tr><th>iter</th>{keys.map((k) => <th key={k}>{k.split(".").slice(-1)[0]}</th>)}<th>{metric}</th>{cons.map((c) => <th key={c.metric}>{c.metric}</th>)}<th>상태</th></tr></thead>
              <tbody>{search.map((r) => (
                <tr key={r.run_id} className={r.run_id === x.best_run_id ? "best-row" : ""}>
                  <td>{r.iteration_index}{r.run_id === x.best_run_id && " ★"}</td>
                  {keys.map((k) => <td key={k}>{fmt(r.param_values[k])}</td>)}
                  <td><b>{fmt(r.metrics?.[metric])}</b></td>
                  {cons.map((c) => {
                    const v = r.metrics?.[c.metric];
                    const okc = v !== undefined && (c.op === ">=" ? v >= c.value : c.op === "<=" ? v <= c.value : c.op === ">" ? v > c.value : v < c.value);
                    return <td key={c.metric} className={okc ? "" : "bad"}>{fmt(v)}</td>;
                  })}
                  <td>{r.status === "evaluated" ? <span className="muted sm">{fmt(r.duration_s, 2)}s</span> : <span className="pill bad" title={r.error || ""}>실패</span>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Card>

      {final.data && (
        <Card title="최종 결과" actions={final.data.report_md_uri && <button className="ghost" onClick={() => setReport((v) => !v)}>{report ? "리포트 닫기" : "리포트 보기"}</button>}>
          <div className="row gap wrap">
            <div><div className="muted sm">종료 사유</div><div className="big-s">{final.data.stop_reason || "–"}</div></div>
            <div><div className="muted sm">최적 {metric}</div><div className="big">{fmt(final.data.metrics?.[metric])}</div></div>
            {cons.map((c) => <div key={c.metric}><div className="muted sm">{c.metric} ({opText(c.op)} {c.value})</div><div className="big-s">{fmt(final.data?.metrics?.[c.metric])}</div></div>)}
          </div>
          {final.data.validation && Object.keys(final.data.validation).length > 0 && (
            <>
              <h3>검증 스위트 (held-out)</h3>
              <table className="compact"><thead><tr><th>스위트</th><th>기본값 {metric}</th><th>최적값 {metric}</th>
                {cons.map((c) => <th key={c.metric}>최적값 {c.metric}</th>)}<th>판정</th></tr></thead>
                <tbody>{Object.entries(final.data.validation).map(([s, v]) => {
                  const bm = v.best?.metrics || {};
                  const verdict = v.improved === null ? <span className="muted">–</span>
                    : v.best && !v.best.feasible ? <span className="pill warn" title="튜닝 스위트에서는 만족했지만 held-out에서 제약을 어겼습니다">✕ 제약 위반 (일반화 실패)</span>
                    : v.improved ? <span className="pill ok">✓ 개선 유지</span>
                    : <span className="pill warn" title="held-out에서 기본값보다 나쁩니다">✕ 악화 (과적합 의심)</span>;
                  return (
                    <tr key={s}><td>{s}</td><td>{fmt(v.baseline?.objective)}</td><td><b>{fmt(v.best?.objective)}</b></td>
                      {cons.map((c) => <td key={c.metric} className={v.best && !feasible({ ...o!, constraints: [c] }, bm) ? "bad" : ""}>{fmt(bm[c.metric])}</td>)}
                      <td>{verdict}</td></tr>
                  );
                })}</tbody></table>
            </>
          )}
          {report && final.data.report_md_uri && <TextArtifact uri={final.data.report_md_uri} />}
          {validation.length > 0 && <p className="muted sm">검증 런 {validation.length}개</p>}
        </Card>
      )}
      {final.data && done && x.status !== "failed" && <Promotion e={x} final={final.data} reload={final.reload} />}

      {!!iters.data?.length && (
        <Card title="에이전트 판단 기록">
          <ol className="notes">{iters.data.map((it) => (
            <li key={`${it.attempt}-${it.iteration}`}><span className="muted sm">#{it.iteration} {it.pm_action}{it.stop_reason ? ` (${it.stop_reason})` : ""}</span> — {it.analyst_findings}<div className="muted sm">{it.rationale}</div></li>
          ))}</ol>
        </Card>
      )}
    </div>
  );
}
