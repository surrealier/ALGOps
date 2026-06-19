"""The parameter-externalization contract types (plan §2A / §5).

A parameter is always a fully-specified object (never a bare scalar) so the PM can reason
about range/step/influence and so it maps cleanly onto Neo4j ``:Parameter`` nodes later.
Keys are dotted, globally unique, and IMMUTABLE (renaming breaks lineage + learned weights).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .enums import Dtype, Influence

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class InfluenceTags(BaseModel):
    """A-priori belief about how a parameter moves false-positive / false-negative rates."""

    fp: Influence = Influence.unknown
    fn: Influence = Influence.unknown


class ParamDef(BaseModel):
    """One row of ``params.yaml`` — the externalized definition of a single knob."""

    key: str = Field(description="dotted, globally unique, immutable")
    dtype: Dtype
    default: Any
    range: tuple[float, float] | None = None  # required for float/int
    step: float | None = None  # search granularity for the PM (required for float/int)
    enum: list[Any] | None = None  # required for enum
    stage: str  # must match a CSV stage id (the join key)
    influence: InfluenceTags = Field(default_factory=InfluenceTags)
    unit: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ParamDef":
        if not KEY_PATTERN.match(self.key):
            raise ValueError(f"invalid param key {self.key!r}: must match {KEY_PATTERN.pattern}")
        if self.dtype in (Dtype.float, Dtype.int):
            if self.range is None or self.step is None:
                raise ValueError(f"{self.key}: float/int params require range and step")
            lo, hi = self.range
            if not (lo <= float(self.default) <= hi):
                raise ValueError(f"{self.key}: default {self.default} outside range {self.range}")
        if self.dtype is Dtype.enum:
            if not self.enum:
                raise ValueError(f"{self.key}: enum params require a non-empty 'enum'")
            if self.default not in self.enum:
                raise ValueError(f"{self.key}: default {self.default!r} not in enum {self.enum}")
        return self

    def clamp(self, value: Any) -> Any:
        """Constrain a proposed value into the param's legal space."""
        if self.dtype in (Dtype.float, Dtype.int) and self.range is not None:
            lo, hi = self.range
            value = max(lo, min(hi, float(value)))
            if self.dtype is Dtype.int:
                value = int(round(value))
        return value


class ParamSpec(BaseModel):
    """The full set of externalized parameters for a project (one ``params.yaml``)."""

    version: int = 1
    modality: str = "tabular"
    params: list[ParamDef] = Field(default_factory=list)

    def by_key(self) -> dict[str, ParamDef]:
        return {p.key: p for p in self.params}

    def defaults(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params}


class ParamChange(BaseModel):
    """A single proposed delta, emitted by the PM and applied by the Developer."""

    key: str
    old: Any
    new: Any
    direction: str  # "up" | "down" | "set"
