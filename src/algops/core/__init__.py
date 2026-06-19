"""algops.core — contracts: the single source of truth for ids, enums, params, schemas, config."""

from __future__ import annotations

from .config import LLMConfig, MasterSettings, SlaveSettings
from .enums import (
    CommandKind,
    Dtype,
    ExperimentStatus,
    GoalDirection,
    Influence,
    MetricName,
    Modality,
    PMAction,
    RunStatus,
    SlaveStatus,
    StopReason,
    TERMINAL_STATUSES,
)
from .ids import new_id, new_token, uuid7
from .params import InfluenceTags, KEY_PATTERN, ParamChange, ParamDef, ParamSpec
from .schemas import (
    CommandResultUpload,
    ExperimentCreate,
    ExperimentOut,
    FinalResultOut,
    HeartbeatRequest,
    Metrics,
    NextCommandResponse,
    PMDecision,
    ProjectCreate,
    ProjectOut,
    RunOut,
    RunRequest,
    RunResult,
    SlaveRegisterRequest,
    SlaveRegisterResponse,
)

__all__ = [
    "LLMConfig", "MasterSettings", "SlaveSettings",
    "CommandKind", "Dtype", "ExperimentStatus", "GoalDirection", "Influence",
    "MetricName", "Modality", "PMAction", "RunStatus", "SlaveStatus", "StopReason",
    "TERMINAL_STATUSES",
    "new_id", "new_token", "uuid7",
    "InfluenceTags", "KEY_PATTERN", "ParamChange", "ParamDef", "ParamSpec",
    "CommandResultUpload", "ExperimentCreate", "ExperimentOut", "FinalResultOut",
    "HeartbeatRequest", "Metrics", "NextCommandResponse", "PMDecision",
    "ProjectCreate", "ProjectOut", "RunOut", "RunRequest", "RunResult",
    "SlaveRegisterRequest", "SlaveRegisterResponse",
]
