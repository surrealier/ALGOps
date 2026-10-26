"""Promotion — propose a campaign's winning parameters back to git (git is the source of truth).

The runner writes the new defaults on a branch at the campaign's pinned commit and returns the
patch; if pushing is enabled the master can open a pull/merge request on GitHub or GitLab. A
human reviews and merges — ALGOps never changes a default branch by itself.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from algops.core import MasterSettings, Objective, PromoteIn, PromoteRequest, normalize_metrics
from algops.tracking import get_session, repo

from ..context import MasterContext
from ..dispatch import repo_ref

def repo_path(url: str) -> str | None:
    """``owner/name`` (GitHub) or ``group/sub/name`` (GitLab) from an https or ssh git URL."""
    url = url.strip()
    m = re.match(r"^(?:https?://|ssh://)(?:[^@/]+@)?[^/]+/(?P<path>.+?)(?:\.git)?/?$", url)
    if m:
        return m.group("path")
    m = re.match(r"^[^@]+@[^:]+:(?P<path>.+?)(?:\.git)?/?$", url)
    if m:
        return m.group("path")
    return None


def _commit_message(algorithm_id: str, objective: Objective | None, changed: dict[str, dict],
                    metrics: dict[str, float], experiment_id: str, attempt: int) -> str:
    head = f"algops: tune {algorithm_id} parameters"
    if objective is not None:
        head += f" ({objective.metric} {objective.direction.value})"
    lines = [head, ""]
    lines += [f"- {k}: {c.get('old')} -> {c.get('new')}" for k, c in changed.items()]
    if metrics:
        shown = [objective.metric] if objective else []
        shown += [c.metric for c in (objective.constraints if objective else [])]
        shown += [n for n in ("precision", "recall", "f1") if n not in shown]
        vals = ", ".join(f"{n}={metrics[n]:.4f}" for n in shown if n in metrics)
        if vals:
            lines += ["", f"Best on tuning suite: {vals}"]
    lines += ["", f"ALGOps-Experiment: {experiment_id}", f"ALGOps-Attempt: {attempt}"]
    return "\n".join(lines)


async def open_pull_request(settings: MasterSettings, *, repo_url: str, branch: str, base: str | None,
                            title: str, body: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    provider = (settings.git_provider or "").lower()
    token = settings.git_provider_token
    path = repo_path(repo_url)
    if provider not in ("github", "gitlab") or not token:
        return {"error": "git_provider / git_provider_token are not configured on the master"}
    if not path:
        return {"error": f"cannot parse repository path from {repo_url!r}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        if provider == "github":
            api = (settings.git_provider_api_url or "https://api.github.com").rstrip("/")
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
            if not base:
                r = await client.get(f"{api}/repos/{path}", headers=headers)
                r.raise_for_status()
                base = r.json().get("default_branch") or "main"
            r = await client.post(f"{api}/repos/{path}/pulls", headers=headers,
                                  json={"title": title, "head": branch, "base": base, "body": body})
            if r.status_code >= 400:
                return {"error": f"GitHub {r.status_code}: {r.text[:300]}"}
            return {"url": r.json().get("html_url"), "number": r.json().get("number")}
        api = (settings.git_provider_api_url or "https://gitlab.com/api/v4").rstrip("/")
        headers = {"PRIVATE-TOKEN": token}
        pid = quote(path, safe="")
        if not base:
            r = await client.get(f"{api}/projects/{pid}", headers=headers)
            r.raise_for_status()
            base = r.json().get("default_branch") or "main"
        r = await client.post(f"{api}/projects/{pid}/merge_requests", headers=headers,
                              json={"source_branch": branch, "target_branch": base, "title": title,
                                    "description": body})
        if r.status_code >= 400:
            return {"error": f"GitLab {r.status_code}: {r.text[:300]}"}
        return {"url": r.json().get("web_url"), "number": r.json().get("iid")}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if own:
            await client.aclose()


async def promote_experiment(ctx: MasterContext, experiment_id: str, body: PromoteIn) -> dict[str, Any]:
    assert ctx.dispatcher is not None
    with get_session() as s:
        exp = repo.get_experiment(s, experiment_id)
        fr = repo.get_final_result(s, experiment_id)
        if exp is None or fr is None:
            return {"ok": False, "error": "experiment has no final result yet"}
        proj = repo.get_project(s, exp.project_id)
        if proj is None:
            return {"ok": False, "error": "project not found"}
        changed = dict(fr.changed_params or {})
        # The base is the commit the *winning attempt* ran on — not the experiment's current pin,
        # which a later re-run may have moved to a newer commit.
        base_sha = fr.commit_sha or (exp.commit_sha if fr.attempt == exp.attempt else None)
        if not base_sha:
            return {"ok": False, "error": "the final result does not record its commit — re-run the campaign"}
        ref = repo_ref(proj, sha=base_sha)
        objective = Objective.model_validate(exp.objective) if exp.objective else None
        metrics = normalize_metrics(fr.metrics)
        attempt = fr.attempt
        algorithm_id = exp.algorithm_id
        repo_url, default_ref, name = proj.repo_url, proj.default_ref, exp.name
        project_id = exp.project_id
    if not changed:
        return {"ok": False, "error": "the best parameters equal the current defaults — nothing to propose"}
    # UUIDv7 ids share their leading (timestamp) characters for ~a minute: use the random tail.
    branch = f"{ctx.settings.promote_branch_prefix}{algorithm_id}/{experiment_id.replace('-', '')[-12:]}-a{attempt}"
    message = _commit_message(algorithm_id, objective, changed, metrics, experiment_id, attempt)
    push = body.push if body.push is not None else (ctx.settings.promote_push or body.pull_request)
    req = PromoteRequest(project_id=project_id, repo=ref, algorithm_id=algorithm_id,
                         params={k: c["new"] for k, c in changed.items()}, branch=branch,
                         message=message, push=bool(push))
    res = await ctx.dispatcher.promote(req)
    record: dict[str, Any] = res.model_dump(mode="json")
    patch = record.pop("patch", None)
    if patch:
        record["patch_uri"] = ctx.store.put_text(f"experiments/{experiment_id}/attempt-{attempt}/promotion.patch", patch)
    if res.ok and body.pull_request:
        if not res.pushed:
            record["pr_error"] = "branch was not pushed (enable push / check runner credentials)"
        elif ref.kind != "git" or not repo_url:
            record["pr_error"] = "pull requests need a git project"
        else:
            title = body.title or f"ALGOps: {name} — tune {algorithm_id}"
            pr = await open_pull_request(ctx.settings, repo_url=repo_url, branch=branch,
                                         base=body.base or default_ref, title=title, body=message)
            record["pr_url"] = pr.get("url")
            if pr.get("error"):
                record["pr_error"] = pr["error"]
    with get_session() as s:
        repo.set_promotion(s, experiment_id, record)
    await ctx.ws.publish(experiment_id, {"type": "promoted", "experiment_id": experiment_id, **record})
    return record
