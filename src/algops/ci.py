"""``algops ci`` — trigger a benchmark on a master from any CI system and gate on its verdict.

    algops ci --master http://algops:8000 --project vision --ref "$GIT_COMMIT"   # exit 1 on failure

Exit codes: 0 passed · 1 failed (gate / regression / error cell) · 2 usage/API error · 3 timeout.
"""

from __future__ import annotations

import json
import sys
import time

import httpx

_ICON = {"passed": "✅", "improved": "⬆️ ", "regressed": "⚠️ ", "gate_failed": "❌", "error": "💥", "pending": "…"}


def _fmt(metrics: dict | None) -> str:
    if not metrics:
        return "-"
    return ", ".join(f"{k}={v:.4g}" for k, v in list(metrics.items())[:5])


class _ApiFailure(Exception):
    pass


_TRANSIENT = (429, 500, 502, 503, 504)


def _get_json(c: httpx.Client, url: str, *, retries: int = 8, backoff_s: float = 2.0) -> dict:
    """GET + decode with retries for proxies/restarts (5xx, HTML error pages, dropped connections).

    Persistent failure raises ``_ApiFailure`` -> exit 2 ("could not determine the verdict"),
    never an exit 1 that would read as "the benchmark failed".
    """
    last = "no response"
    for attempt in range(retries + 1):
        try:
            r = c.get(url)
            if r.status_code in _TRANSIENT:
                last = f"HTTP {r.status_code}"
            elif r.status_code >= 400:
                raise _ApiFailure(f"GET {url}: HTTP {r.status_code} {r.text[:200]}")
            else:
                try:
                    return r.json()
                except ValueError:
                    last = f"non-JSON response ({r.text[:80]!r})"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            print(f"warning: GET {url} failed ({last}); retrying", file=sys.stderr, flush=True)
            time.sleep(min(30.0, backoff_s * (attempt + 1)))
    raise _ApiFailure(f"GET {url}: {last} (after {retries + 1} attempts)")


def run_ci(*, master_url: str, token: str | None, project: list[str] | None, ref: str | None,
           algorithms: list[str] | None, suites: list[str] | None, tags: list[str] | None,
           wait: bool = True, timeout_s: float = 3600.0, json_out: str | None = None,
           poll_s: float = 3.0, client: httpx.Client | None = None) -> int:
    if ref and not project:
        print("error: --ref needs --project (a ref only makes sense for specific repositories)", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    own = client is None
    c = client or httpx.Client(base_url=master_url.rstrip("/"), headers=headers, timeout=60.0)
    try:
        scope = None
        if project:
            scope = []
            for ident in project:
                r = c.get(f"/api/v1/projects/{ident}")
                if r.status_code == 404:
                    print(f"error: project {ident!r} not found on {master_url}", file=sys.stderr)
                    return 2
                item: dict = {"project_id": _get_json(c, f"/api/v1/projects/{ident}")["project_id"]}
                if ref:
                    item["ref"] = ref
                if algorithms:
                    item["algorithms"] = algorithms
                if suites:
                    item["suites"] = suites
                if tags:
                    item["tags"] = tags
                scope.append(item)
        name = "ci" + (f" {ref[:12]}" if ref else "")
        r = c.post("/api/v1/benchmarks", json={"name": name, "scope": scope, "trigger": "api"})
        if r.status_code >= 400:
            print(f"error: {r.status_code} {r.text[:300]}", file=sys.stderr)
            return 2
        try:
            bid = r.json()["benchmark_id"]
        except (ValueError, KeyError):
            print(f"error: unexpected response {r.status_code} {r.text[:200]!r}", file=sys.stderr)
            return 2
        print(f"benchmark {bid} started on {master_url}", flush=True)
        if not wait:
            return 0
        deadline = time.monotonic() + timeout_s
        doc = None
        while time.monotonic() < deadline:
            doc = _get_json(c, f"/api/v1/benchmarks/{bid}", backoff_s=max(poll_s, 0.01))
            if doc.get("status") in ("passed", "failed", "error"):
                break
            time.sleep(poll_s)
        else:
            print(f"timeout: benchmark {bid} still {doc and doc['status']}", file=sys.stderr)
            return 3
        assert doc is not None
        for cell in doc.get("cells") or []:
            icon = _ICON.get(cell["verdict"], "?")
            line = (f"{icon} {cell['verdict']:<11} {cell.get('project_name') or cell['project_id'][:8]}:"
                    f"{cell['algorithm_id']}@{cell['suite_id']}  {_fmt(cell.get('metrics'))}")
            if cell.get("error"):
                line += f"  {cell['error'].splitlines()[0][:160]}"
            for g in cell.get("gates") or []:
                if not g["passed"]:
                    line += f"\n      gate {g['metric']} {g['op']} {g['value']} (actual {g['actual']})"
            for m, d in (cell.get("deltas") or {}).items():
                if d.get("status") == "regressed":
                    line += f"\n      regressed {m}: {d['baseline']:.4g} → {d['current']:.4g}"
                elif d.get("status") == "lost":
                    line += f"\n      regressed {m}: {d['baseline']:.4g} → (not reported)"
            print(line)
        summary = ", ".join(f"{k}={v}" for k, v in (doc.get("summary") or {}).items() if v)
        print(f"\n{doc['status'].upper()} — {summary}")
        if json_out:
            with open(json_out, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2)
        return 0 if doc["status"] == "passed" else 1
    except (httpx.HTTPError, _ApiFailure, ValueError, KeyError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if own:
            c.close()
