import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, api } from "./api";

// ---------------------------------------------------------------- formatting

export const fmt = (v: unknown, digits = 4): string => {
  if (v === null || v === undefined) return "–";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return "–";
    if (Number.isInteger(v) && Math.abs(v) >= 1) return v.toLocaleString("ko-KR");
    return Number(v.toPrecision(digits)).toString();
  }
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
};

export const short = (sha?: string | null, n = 8) => (sha ? sha.slice(0, n) : "–");

export function ago(iso?: string | null): string {
  if (!iso) return "–";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 0) return new Date(iso).toLocaleString("ko-KR");
  if (s < 60) return `${Math.floor(s)}초 전`;
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return new Date(iso).toLocaleDateString("ko-KR");
}

export const opText = (op: string) => ({ ">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "=" }[op] ?? op);

// ---------------------------------------------------------------- badges

const VERDICT: Record<string, { label: string; cls: string; icon: string }> = {
  passed: { label: "통과", cls: "ok", icon: "✓" },
  improved: { label: "개선", cls: "acc", icon: "▲" },
  regressed: { label: "회귀", cls: "warn", icon: "▼" },
  gate_failed: { label: "게이트 실패", cls: "bad", icon: "✕" },
  error: { label: "오류", cls: "bad", icon: "!" },
  pending: { label: "대기", cls: "", icon: "…" },
};

export function VerdictBadge({ v, compact }: { v: string; compact?: boolean }) {
  const d = VERDICT[v] ?? { label: v, cls: "", icon: "?" };
  return <span className={`pill ${d.cls}`} title={v}>{d.icon}{compact ? "" : ` ${d.label}`}</span>;
}

const STATUS: Record<string, { label: string; cls: string }> = {
  created: { label: "생성됨", cls: "" },
  running: { label: "실행 중", cls: "acc" },
  paused: { label: "승인 대기", cls: "warn" },
  target_reached: { label: "목표 달성", cls: "ok" },
  stopped_max_iter: { label: "최대 반복 도달", cls: "ok" },
  stopped_by_pm: { label: "탐색 종료", cls: "ok" },
  failed: { label: "실패", cls: "bad" },
  passed: { label: "통과", cls: "ok" },
  error: { label: "오류", cls: "bad" },
};

export function StatusPill({ s }: { s: string }) {
  const d = STATUS[s] ?? { label: s, cls: "" };
  return <span className={`pill ${d.cls}`}>{s === "running" && <span className="spin" />}{d.label}</span>;
}

export function Sha({ sha }: { sha?: string | null }) {
  if (!sha) return <span className="muted">–</span>;
  const dirty = sha.includes("-dirty") || sha.startsWith("local");
  return <code className={dirty ? "dirty" : ""} title={sha}>{sha.startsWith("local") ? sha : short(sha)}</code>;
}

// ---------------------------------------------------------------- layout

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className ?? ""}`}>
      {(title || actions) && (
        <div className="card-head">
          {title && <h2>{title}</h2>}
          <div className="row gap">{actions}</div>
        </div>
      )}
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}{hint && <em> — {hint}</em>}</span>
      {children}
    </label>
  );
}

export function Tiles({ items }: { items: { label: string; value: ReactNode; tone?: string }[] }) {
  return (
    <div className="tiles">
      {items.map((t) => (
        <div key={t.label} className={`tile ${t.tone ?? ""}`}>
          <div className="tile-label">{t.label}</div>
          <div className="tile-value">{t.value}</div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- data hooks

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) askToken();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    reload();
    if (!pollMs) return;
    const t = setInterval(reload, pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

// Global error/notice banner + API-token prompt on 401.
type Notice = { kind: "error" | "ok"; text: string };
let pushNotice: (n: Notice) => void = () => {};
let askToken: () => void = () => {};

export function notify(kind: Notice["kind"], text: string) { pushNotice({ kind, text }); }

export async function act<T>(fn: () => Promise<T>, okText?: string): Promise<T | null> {
  try {
    const r = await fn();
    if (okText) notify("ok", okText);
    return r;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) askToken();
    notify("error", e instanceof Error ? e.message : String(e));
    return null;
  }
}

export function NoticeHost({ onAskToken }: { onAskToken: () => void }) {
  const [items, setItems] = useState<(Notice & { id: number })[]>([]);
  useEffect(() => {
    pushNotice = (n) => {
      const id = Date.now() + Math.random();
      setItems((xs) => [...xs.slice(-3), { ...n, id }]);
      setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), n.kind === "error" ? 8000 : 3000);
    };
    askToken = onAskToken;
  }, [onAskToken]);
  return (
    <div className="toasts" aria-live="polite">
      {items.map((n) => <div key={n.id} className={`toast ${n.kind}`}>{n.text}</div>)}
    </div>
  );
}

// ---------------------------------------------------------------- charts

/** Objective per run (dots) + best-so-far (step line). Lower/higher is better per `minimize`. */
export function ProgressChart({ points, minimize, target, height = 240 }: {
  points: { x: number; y: number | null; ok: boolean; feasible: boolean; label: string }[];
  minimize: boolean; target?: number | null; height?: number;
}) {
  const W = 1040, H = height, P = { l: 56, r: 14, t: 14, b: 28 };
  const ys = points.map((p) => p.y).filter((y): y is number => y !== null);
  if (ys.length === 0) return <Empty>아직 평가된 런이 없습니다.</Empty>;
  const all = target != null ? [...ys, target] : ys;
  let lo = Math.min(...all), hi = Math.max(...all);
  if (lo === hi) { lo -= 0.5 * (Math.abs(lo) || 1); hi += 0.5 * (Math.abs(hi) || 1); }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const n = Math.max(2, points.length);
  const X = (i: number) => P.l + (i / (n - 1)) * (W - P.l - P.r);
  const Y = (v: number) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
  let best: number | null = null;
  const bestPath: string[] = [];
  points.forEach((p, i) => {
    if (p.y !== null && p.ok && p.feasible && (best === null || (minimize ? p.y < best : p.y > best))) best = p.y;
    if (best !== null) bestPath.push(`${bestPath.length ? "L" : "M"}${X(i).toFixed(1)},${Y(best).toFixed(1)}`);
  });
  const ticks = [lo + pad, (lo + hi) / 2, hi - pad];
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="탐색 진행 차트">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={P.l} x2={W - P.r} y1={Y(t)} y2={Y(t)} className="grid" />
          <text x={P.l - 6} y={Y(t) + 4} textAnchor="end" className="axis">{fmt(t, 3)}</text>
        </g>
      ))}
      {target != null && (
        <g>
          <line x1={P.l} x2={W - P.r} y1={Y(target)} y2={Y(target)} className="target" />
          <text x={W - P.r} y={Y(target) - 4} textAnchor="end" className="axis target-label">목표 {fmt(target, 3)}</text>
        </g>
      )}
      <path d={bestPath.join(" ")} className="best" />
      {points.map((p, i) => p.y === null ? (
        <text key={i} x={X(i)} y={H - P.b - 4} textAnchor="middle" className="axis bad">✕</text>
      ) : (
        <circle key={i} cx={X(i)} cy={Y(p.y)} r={4} className={p.feasible ? "dot" : "dot infeasible"}>
          <title>{p.label}</title>
        </circle>
      ))}
      <text x={P.l} y={H - 6} className="axis">런 순서 →</text>
    </svg>
  );
}

/** Tiny trend line for a metric across benchmark history (oldest → newest). */
export function Sparkline({ values, width = 120, height = 28 }: { values: (number | null)[]; width?: number; height?: number }) {
  const v = values.filter((x): x is number => x !== null);
  if (v.length < 2) return <span className="muted sm">–</span>;
  const lo = Math.min(...v), hi = Math.max(...v);
  const span = hi - lo || 1;
  const pts = values.map((x, i) => (x === null ? null : `${(i / (values.length - 1)) * (width - 4) + 2},${height - 3 - ((x - lo) / span) * (height - 6)}`))
    .filter(Boolean).join(" ");
  return (
    <svg width={width} height={height} className="spark" aria-hidden="true">
      <polyline points={pts} />
    </svg>
  );
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="card-head"><h2>{title}</h2><button className="ghost" onClick={onClose} aria-label="닫기">✕</button></div>
        {children}
      </div>
    </div>
  );
}

export function TextArtifact({ uri }: { uri: string }) {
  const [text, setText] = useState<string>("불러오는 중…");
  useEffect(() => {
    api.artifact(uri).then(setText).catch((e) => {
      if (e instanceof ApiError && e.status === 401) { askToken(); setText("API 토큰이 필요합니다."); }
      else setText("불러오지 못했습니다.");
    });
  }, [uri]);
  return <pre className="artifact">{text}</pre>;
}
