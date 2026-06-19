"""Runtime configuration for the master and slave processes (pydantic-settings).

Every component reaches the LLM only through an OpenAI-compatible ``/v1`` endpoint, so
"external vLLM / external Ollama / master-spun vLLM" collapses to a single ``LLMConfig``.
M0 does not call the LLM, but the struct is fixed now so M1 only flips a flag.
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # One-time enroll token presented by slaves at registration (rotate in prod / per-UI).
    enroll_token: str = "dev-enroll-token"

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


class SlaveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALGOPS_SLAVE_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8100

    master_url: str = "http://127.0.0.1:8000"
    enroll_token: str = "dev-enroll-token"

    project_name: str = "toy-project"
    modality: str = "tabular"
    host_label: str = "dev-pc"

    # Path to the externalized project (mounted volume in prod). Defaults to the bundled toy.
    project_path: str | None = None

    # Long-poll + heartbeat cadence.
    poll_timeout_s: float = 25.0
    heartbeat_interval_s: float = 10.0
    work_dir: str = "./.algops/slave_work"
