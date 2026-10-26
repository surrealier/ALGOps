"""The parameter-externalization contract types (plan §2A / §5).

A parameter is always a fully-specified object (never a bare scalar) so the PM can reason
about range/step/influence and so it maps cleanly onto Neo4j ``:Parameter`` nodes later.
Keys are dotted, unique within an algorithm, and IMMUTABLE (renaming breaks lineage + learned weights).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import Dtype, Influence

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class InfluenceTags(BaseModel):
    """A-priori belief about how a parameter moves false-positive / false-negative rates."""

    model_config = ConfigDict(extra="forbid")

    fp: Influence = Influence.unknown
    fn: Influence = Influence.unknown


class ParamDef(BaseModel):
    """One row of ``params.yaml`` — the externalized definition of a single knob."""

    model_config = ConfigDict(extra="forbid")  # a typo'd field must not be silently ignored

    key: str = Field(description="dotted, unique within an algorithm, immutable")
    dtype: Dtype
    default: Any
    range: tuple[float, float] | None = None  # required for float/int
    step: float | None = None  # search granularity for the PM (required for float/int)
    enum: list[Any] | None = None  # required for enum
    stage: str = "decision"  # telemetry stage id the parameter acts in
    influence: InfluenceTags = Field(default_factory=InfluenceTags)
    unit: str | None = None
    description: str | None = None
    # Decision drop-reasons (``dec_reason`` values in the telemetry CSV) caused by this param's
    # filter — lets the master attribute drops to parameters without hardcoded mappings.
    drop_reasons: list[str] = Field(default_factory=list)

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

    def coerce(self, value: Any) -> Any:
        """Validate + normalize one value for this param (raises ValueError when illegal)."""
        if self.dtype is Dtype.bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
                return value.lower() in ("true", "1", "yes")
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            raise ValueError(f"{self.key}: {value!r} is not a bool")
        if self.dtype is Dtype.enum:
            if value in (self.enum or []):
                return value
            raise ValueError(f"{self.key}: {value!r} not in {self.enum}")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.key}: {value!r} is not numeric") from None
        clamped = self.clamp(num)
        if self.dtype is Dtype.float:
            return float(round(clamped, 12))
        return clamped

    def grid(self, max_points: int = 64) -> list[Any]:
        """The discrete search grid implied by range/step (or enum / bool choices)."""
        if self.dtype is Dtype.bool:
            return [False, True]
        if self.dtype is Dtype.enum:
            return list(self.enum or [])
        lo, hi = self.range or (float(self.default), float(self.default))
        step = float(self.step or 0) or (hi - lo) or 1.0
        n = int(round((hi - lo) / step)) + 1
        if n > max_points:
            step = (hi - lo) / (max_points - 1)
            n = max_points
        pts = [self.coerce(lo + i * step) for i in range(n)]
        out: list[Any] = []
        for p in pts:
            if p not in out:
                out.append(p)
        return out


class ParamSpec(BaseModel):
    """The full set of externalized parameters for a project (one ``params.yaml``)."""

    version: int = 1
    modality: str = "tabular"
    params: list[ParamDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "ParamSpec":
        keys = [p.key for p in self.params]
        dups = sorted({k for k in keys if keys.count(k) > 1})
        if dups:
            raise ValueError(f"duplicate parameter key(s): {dups}")
        return self

    def by_key(self) -> dict[str, ParamDef]:
        return {p.key: p for p in self.params}

    def defaults(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params}

    def effective(self, overrides: dict[str, Any] | None = None, *, strict: bool = True) -> dict[str, Any]:
        """Defaults with ``overrides`` applied, each value validated/clamped.

        ``strict`` rejects unknown keys (a typo must never silently evaluate the defaults).
        """
        by_key = self.by_key()
        values = {k: by_key[k].coerce(v) for k, v in self.defaults().items()}
        unknown = [k for k in (overrides or {}) if k not in by_key]
        if unknown and strict:
            raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
        for key, value in (overrides or {}).items():
            if key in by_key:
                values[key] = by_key[key].coerce(value)
        return values

    def reason_map(self) -> dict[str, str]:
        """``dec_reason`` -> param key, from each param's declared ``drop_reasons``."""
        out: dict[str, str] = {}
        for p in self.params:
            for reason in p.drop_reasons:
                out.setdefault(reason, p.key)
        return out


class ParamChange(BaseModel):
    """A single proposed delta, emitted by the PM and applied by the Developer."""

    key: str
    old: Any
    new: Any
    direction: str  # "up" | "down" | "set"
