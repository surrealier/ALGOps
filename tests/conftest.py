"""Shared fixtures: throwaway git repositories built from the bundled examples."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "examples")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "algops-test", "GIT_AUTHOR_EMAIL": "test@algops.local",
    "GIT_COMMITTER_NAME": "algops-test", "GIT_COMMITTER_EMAIL": "test@algops.local",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args: str, cwd: str | None = None) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    out = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@dataclass
class GitRepo:
    work: str  # a normal clone we edit + push from
    remote: str  # bare repository (what ALGOps registers, via file://)

    @property
    def url(self) -> str:
        return "file://" + self.remote

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.work)

    def commit_all(self, message: str) -> str:
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", message, cwd=self.work)
        git("push", "-q", "origin", "HEAD:main", cwd=self.work)
        return self.head()


def make_repo(tmp_path, example: str) -> GitRepo:
    work = os.path.join(str(tmp_path), example + "-work")
    remote = os.path.join(str(tmp_path), example + ".git")
    shutil.copytree(os.path.join(EXAMPLES, example), work,
                    ignore=shutil.ignore_patterns("build", "__pycache__", ".algops"))
    git("init", "-q", "--bare", "-b", "main", remote)
    git("init", "-q", "-b", "main", work)
    git("remote", "add", "origin", remote, cwd=work)
    repo = GitRepo(work=work, remote=remote)
    repo.commit_all("initial")
    return repo


@pytest.fixture
def py_repo(tmp_path) -> GitRepo:
    return make_repo(tmp_path, "py-multi-algo")


@pytest.fixture
def c_repo(tmp_path) -> GitRepo:
    if shutil.which("cc") is None:
        pytest.skip("no C compiler")
    return make_repo(tmp_path, "c-threshold")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
