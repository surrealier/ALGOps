import { useCallback, useEffect, useState } from "react";
import { api, token, type Health } from "./api";
import { go, useRoute } from "./nav";
import { Modal, NoticeHost, notify } from "./ui";
import { BenchmarkDetail, BenchmarksView } from "./views/benchmarks";
import { ExperimentDetail, ExperimentsView } from "./views/experiments";
import { OverviewView, RunnersView, SchedulesView } from "./views/ops";
import { ProjectDetail, ProjectsView } from "./views/projects";

const NAV = [
  { path: "", label: "개요" },
  { path: "projects", label: "프로젝트" },
  { path: "benchmarks", label: "벤치마크" },
  { path: "experiments", label: "최적화" },
  { path: "runners", label: "Runner" },
  { path: "schedules", label: "스케줄" },
];

function TokenDialog({ onClose }: { onClose: () => void }) {
  const [v, setV] = useState(token.get());
  return (
    <Modal title="API 토큰" onClose={onClose}>
      <p className="muted sm">이 마스터는 API 토큰을 요구합니다 (<code>ALGOPS_MASTER_API_TOKEN</code>). 이 브라우저에만 저장됩니다.</p>
      <input type="password" value={v} autoFocus onChange={(e) => setV(e.target.value)} placeholder="토큰" />
      <div className="row gap" style={{ marginTop: 10 }}>
        <button onClick={() => { token.set(v.trim()); notify("ok", "토큰을 저장했습니다"); onClose(); window.location.reload(); }}>저장</button>
        <button className="ghost" onClick={() => { token.set(""); setV(""); }}>지우기</button>
      </div>
    </Modal>
  );
}

export default function App() {
  const { parts, query } = useRoute();
  const [health, setHealth] = useState<Health | null>(null);
  const [tokenOpen, setTokenOpen] = useState(false);
  const askToken = useCallback(() => setTokenOpen(true), []);
  useEffect(() => {
    const ping = () => api.health().then(setHealth).catch(() => setHealth(null));
    ping();
    const t = setInterval(ping, 10000);
    return () => clearInterval(t);
  }, []);
  const [section, id] = parts;
  let view;
  switch (section) {
    case "projects": view = id ? <ProjectDetail id={id} /> : <ProjectsView />; break;
    case "benchmarks": view = id ? <BenchmarkDetail id={id} /> : <BenchmarksView />; break;
    case "experiments": view = id ? <ExperimentDetail id={id} /> : <ExperimentsView query={query} />; break;
    case "runners": view = <RunnersView />; break;
    case "schedules": view = <SchedulesView />; break;
    default: view = <OverviewView />;
  }
  return (
    <div className="shell">
      <header className="top">
        <a className="brand" onClick={() => go("/")}>⚙️ ALGOps</a>
        <nav>
          {NAV.map((n) => (
            <a key={n.path} className={(section || "") === n.path ? "on" : ""} onClick={() => go(`/${n.path}`)}>{n.label}</a>
          ))}
        </nav>
        <span className="grow" />
        {health?.auth && <button className="ghost sm-btn" onClick={() => setTokenOpen(true)}>{token.get() ? "🔑 토큰" : "🔒 토큰 입력"}</button>}
        <span className={`health ${health ? "ok" : "bad"}`} title={health ? `master ${health.version}` : "마스터 연결 끊김"}>● {health ? health.version : "offline"}</span>
      </header>
      <main>{view}</main>
      <NoticeHost onAskToken={askToken} />
      {tokenOpen && <TokenDialog onClose={() => setTokenOpen(false)} />}
    </div>
  );
}
