"""ALGOps — RAG-based MLOps for optimizing post-processing algorithm parameters.

M0 walking skeleton. Logical packages live as subpackages here:

    algops.core         # contracts: ids, enums, schemas, params, config (single source of truth)
    algops.tracking     # SQLAlchemy models, session, artifact store, repositories
    algops.extraction   # metric scoring, target checks, CSV summarization
    algops.notify       # deterministic Slack/email/console notifiers
    algops.agents       # LangGraph supervisor loop (M0: deterministic stubs)
    algops.master       # FastAPI control plane + experiment-loop worker
    algops.slave        # FastAPI per-project agent (register / long-poll / run)
    algops.toy_project  # bundled hand-externalized synthetic detector
"""

__version__ = "0.2.0"
