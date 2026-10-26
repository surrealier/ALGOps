"""Git workspaces (mirror + per-commit worktrees) and promotion (branch commit + patch + push)."""

from __future__ import annotations

import os

from algops.core import PromoteRequest, RepoRef, RunRequest, RunStatus, SyncRequest, new_id
from algops.runner import RunnerEngine
from algops.runner.promote import rewrite_defaults

from .conftest import git


async def test_sync_resolves_refs_and_runs_are_pinned_to_commits(tmp_path, py_repo):
    eng = RunnerEngine(str(tmp_path / "w"))
    ref = RepoRef(kind="git", name="py", url=py_repo.url)
    first = await eng.sync(SyncRequest(project_id="p", repo=ref))
    assert first.ok and first.sha == py_repo.head(), first.error
    assert {a["id"] for a in first.manifest["algorithms"]} == {"detector", "smoother"}

    # A new commit changes a default; resolving `main` moves, the old sha still runs the old code.
    path = os.path.join(py_repo.work, "algops", "detector", "params.yaml")
    text = open(path, encoding="utf-8").read().replace("default: 0.5 ", "default: 0.7 ")
    open(path, "w", encoding="utf-8").write(text)
    new_sha = py_repo.commit_all("raise threshold")
    second = await eng.sync(SyncRequest(project_id="p", repo=ref.model_copy(update={"ref": "main"})))
    assert second.sha == new_sha != first.sha

    async def thr(sha: str) -> float:
        out = await eng.run(RunRequest(run_id=new_id(), project_id="p", algorithm_id="detector",
                                       suite_id="det-smoke", repo=ref.model_copy(update={"sha": sha})))
        assert out.result.status == RunStatus.evaluated, out.result.error
        assert out.result.commit_sha == sha
        return out.result.param_values["detector.score_threshold"]

    assert await thr(first.sha) == 0.5
    assert await thr(new_sha) == 0.7
    bad = await eng.sync(SyncRequest(project_id="p", repo=ref.model_copy(update={"ref": "no-such-branch"})))
    assert not bad.ok and "cannot resolve" in bad.error


def test_rewrite_defaults_preserves_comments_and_types(tmp_path):
    p = tmp_path / "params.yaml"
    p.write_text(
        "# header comment\nparams:\n"
        "  - key: a.t\n    dtype: float\n    default: 0.5   # keep me\n    range: [0, 1]\n    step: 0.1\n    stage: s\n"
        "  - key: a.n\n    dtype: int\n    default: 3\n    range: [1, 9]\n    step: 1\n    stage: s\n",
        encoding="utf-8")
    changed = rewrite_defaults(str(p), "x", False, {"a.t": 0.65, "a.n": 5.0})
    text = p.read_text(encoding="utf-8")
    assert changed == {"a.t": {"old": 0.5, "new": 0.65}, "a.n": {"old": 3, "new": 5}}
    assert "# header comment" in text and "# keep me" in text
    assert "default: 5\n" in text  # int stays int
    assert rewrite_defaults(str(p), "x", False, {"a.t": 0.65}) == {}  # idempotent


async def test_promotion_branch_commit_patch_and_push(tmp_path, py_repo):
    eng = RunnerEngine(str(tmp_path / "w"))
    base = py_repo.head()
    ref = RepoRef(kind="git", name="py", url=py_repo.url, sha=base)
    assert (await eng.sync(SyncRequest(project_id="p", repo=ref))).ok
    res = await eng.promote(PromoteRequest(
        project_id="p", repo=ref, algorithm_id="smoother",  # inline params in algops.yaml
        params={"smoother.alpha": 0.45, "smoother.window": 5}, branch="algops/smoother/test",
        message="algops: tune smoother", push=True,
    ))
    assert res.ok, res.error
    assert res.pushed and res.files == ["algops.yaml"]
    assert res.changed["smoother.window"] == {"old": 1, "new": 5}
    assert "+        default: 5" in res.patch or "default: 5" in res.patch
    # The branch landed on the remote, parented on the campaign's base commit; main is untouched.
    remote_branch = git("--git-dir", py_repo.remote, "rev-parse", "algops/smoother/test")
    assert remote_branch == res.commit_sha
    assert git("--git-dir", py_repo.remote, "rev-parse", f"{res.commit_sha}^") == base
    assert git("--git-dir", py_repo.remote, "rev-parse", "main") == base
    shown = git("--git-dir", py_repo.remote, "show", f"{res.commit_sha}:algops.yaml")
    assert "default: 0.45" in shown and "description: EMA weight" in shown  # comments/fields kept

    # Proposing the current defaults is a no-op, not an empty commit.
    noop = await eng.promote(PromoteRequest(
        project_id="p", repo=ref, algorithm_id="smoother", params={"smoother.alpha": 0.9},
        branch="algops/smoother/noop", message="noop"))
    assert not noop.ok and "nothing to propose" in noop.error


async def test_promotion_refuses_uncommitted_local_code(tmp_path):
    eng = RunnerEngine(str(tmp_path / "w"), local_projects={"x": str(tmp_path)})
    res = await eng.promote(PromoteRequest(
        project_id="p", repo=RepoRef(kind="local", name="x", sha="local-abc"), algorithm_id="a",
        params={"a.b": 1}, branch="b", message="m"))
    assert not res.ok and "clean committed base" in res.error
