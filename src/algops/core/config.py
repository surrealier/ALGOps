"""Runtime configuration for the master and slave processes (pydantic-settings).

Every component reaches the LLM only through an OpenAI-compatible ``/v1`` endpoint, so
"external vLLM / external Ollama / master-spun vLLM" collapses to a single ``LLMConfig``.
M0 does not call the LLM, but the struct is fixed now so M1 only flips a flag.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


DEFAULT_ENROLL_TOKEN = "dev-enroll-token"


def _split_csv(v: Any) -> Any:
    """Accept JSON lists *or* comma-separated strings for list-valued env vars."""
    if isinstance(v, str):
        text = v.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]
    return v


def _parse_mapping(v: Any) -> Any:
    """``name=path;name2=path2`` or a JSON object."""
    if isinstance(v, str):
        text = v.strip()
        if not text:
            return {}
        if text.startswith("{"):
            return json.loads(text)
        out: dict[str, str] = {}
        for part in text.split(";"):
            if "=" in part:
                name, path = part.split("=", 1)
                out[name.strip()] = path.strip()
        return out
    return v


class LLMConfig(BaseModel):
    mode: str = "none"  # none | external | self_hosted
    base_url: str | None = None  # OpenAI-compatible /v1 base
    model: str | None = None  # e.g. "Qwen/Qwen3-8B-Instruct"
    api_key: str | None = None


class MasterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALGOPS_MASTER_", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8000

    # Tracking metadata DB. Dev default = SQLite file; prod = postgresql+psycopg://...
    db_url: str = "sqlite+pysqlite:///./.algops/tracking.sqlite"

    # Artifact store. Dev default = local FS; prod = MinIO/S3 (proxied-artifact mode).
    artifact_backend: str = "local"  # local | s3
    artifact_dir: str = "./.algops/artifacts"
    s3_endpoint: str | None = None
    s3_bucket: str = "algops"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # LangGraph checkpointer. Dev = sqlite; prod = postgres (same Postgres as tracking).
    checkpoint_backend: str = "sqlite"  # sqlite | postgres
    checkpoint_db: str = "./.algops/checkpoints.sqlite"

    # Enroll token presented by runners at registration. The default only works in dev_mode.
    enroll_token: str = DEFAULT_ENROLL_TOKEN
    # Browser origins allowed to call the API cross-origin. None => "*" in dev_mode, none otherwise
    # (the UI is served by the master itself, same-origin).
    cors_origins: Annotated[list[str] | None, NoDecode] = None

    # How long a run command may take before the loop gives up on the slave.
    command_timeout_s: float = 600.0

    # Dev mode runs the experiment loop in-process via BackgroundTasks (no Celery/Redis).
    dev_mode: bool = True
    celery_broker_url: str = "redis://localhost:6379/0"
    # Cross-process command bus + live progress. 'memory' (in-process dev) or 'redis' (prod).
    command_bus: str = "memory"  # memory | redis
    ws_backend: str = "memory"  # memory | redis
    require_approval: bool = False  # HITL: pause before the Developer mutates code (M5)

    # Default LLM endpoint (unused in M0).
    llm: LLMConfig = LLMConfig()

    # --- v2: API auth, dispatch, benchmarks, triggers, promotion ---
    # When set, every mutating /api/v1 endpoint (except the slave protocol) needs
    # `Authorization: Bearer <api_token>` (CI, schedulers, the UI after login).
    api_token: str | None = None
    run_retries: int = 1  # re-dispatch a run to another runner when its runner dies (never on timeout)
    queue_timeout_s: float = 86400.0  # give up on a command no runner picked up within this time
    benchmark_max_parallel: int = 0  # 0 => total online runner concurrency
    benchmark_rel_tolerance: float | None = None  # override every algorithm's regression tolerance
    scheduler_enabled: bool = True  # cron + git-watch loop in this process (enable on ONE master)
    scheduler_tick_s: float = 20.0
    sync_timeout_s: float = 300.0
    # Promotion = propose the campaign's best params as a branch commit on the repo.
    promote_push: bool = False  # push the branch to the runner's `origin`
    promote_branch_prefix: str = "algops/"
    git_provider: str | None = None  # github | gitlab (PR creation from the master)
    git_provider_token: str | None = None
    git_provider_api_url: str | None = None  # e.g. https://gitlab.corp/api/v4 (defaults per provider)

    # RAG (M3). Dev default = in-memory; prod = Qdrant + Neo4j.
    rag_backend: str = "memory"  # memory | qdrant_neo4j
    rag_top_k: int = 5
    rag_embed: str = "hashing"  # hashing | fastembed
    rag_rerank: str = "noop"  # noop | bge
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j"

    # Notifications (deterministic). Empty => console fallback.
    slack_bot_token: str | None = None
    slack_channel: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    notify_email_to: str | None = None
    # Optional MCP-authored prose summary, layered ON TOP of the deterministic alert (M5).
    mcp_notify: bool = False
    mcp_server_url: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _origins(cls, v: Any) -> Any:
        return _split_csv(v)


class SlaveSettings(BaseSettings):
    """A runner ("slave"): executes runs for any number of repositories, outbound-only."""

    model_config = SettingsConfigDict(env_prefix="ALGOPS_SLAVE_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8100

    master_url: str = "http://127.0.0.1:8000"
    enroll_token: str = "dev-enroll-token"

    project_name: str = "toy-project"  # name for `project_path` (v1 single-project mode)
    modality: str = "tabular"
    host_label: str = "dev-pc"

    # v1: one externalized project at a local path. Empty => the bundled toy (if serve_toy).
    project_path: str | None = None
    # v2: more local projects, "name=/path;name2=/path2" or JSON {"name": "/path"}.
    local_projects: Annotated[dict[str, str], NoDecode] = {}
    serve_toy: bool = True  # serve the bundled toy detector when no local project is set

    # Capabilities used by the master's dispatcher.
    labels: Annotated[list[str], NoDecode] = []  # e.g. "gpu,cuda12,linux"
    concurrency: int = 1  # runs executed in parallel on this host
    allow_projects: Annotated[list[str] | None, NoDecode] = None  # project names (None => any)
    git: bool = True  # clone/checkout git projects

    # Long-poll + heartbeat cadence.
    poll_timeout_s: float = 25.0
    heartbeat_interval_s: float = 10.0
    work_dir: str = "./.algops/slave_work"
    keep_checkouts: int = 20  # per project; oldest worktrees are pruned beyond this

    # Environment hygiene: variables matching these globs are withheld from repository code
    # (env.setup + entrypoints). Defaults: the runner's own settings and credential-like names.
    # Re-allow what your algorithms legitimately need, e.g. ALGOPS_SLAVE_PASS_ENV="AWS_*,HF_TOKEN".
    pass_env: Annotated[list[str], NoDecode] = []
    withhold_env: Annotated[list[str] | None, NoDecode] = None  # None => built-in defaults

    # Promotion commits.
    git_author_name: str = "ALGOps"
    git_author_email: str = "algops@localhost"
    push_remote: str = "origin"

    @field_validator("labels", "allow_projects", "pass_env", "withhold_env", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _split_csv(v)

    @field_validator("local_projects", mode="before")
    @classmethod
    def _mapping(cls, v: Any) -> Any:
        return _parse_mapping(v)
