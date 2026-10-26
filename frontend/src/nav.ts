// Minimal hash router: "#/benchmarks/<id>?x=1" -> { parts: ["benchmarks", "<id>"], query }.
import { useEffect, useState } from "react";

export function go(path: string) {
  window.location.hash = path.startsWith("#") ? path : `#${path}`;
}

type Route = { parts: string[]; query: URLSearchParams };

function parse(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?", 2);
  return { parts: (path || "").split("/").filter(Boolean), query: new URLSearchParams(qs || "") };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}
