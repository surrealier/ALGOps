"""Contracts: metrics math, param validation, id format, schema round-trips."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from algops.core import Dtype, Metrics, ParamDef, RunResult, RunStatus, new_id


def test_metrics_from_counts():
    m = Metrics.from_counts(tp=8, fp=2, fn=1, tn=9)
    assert m.precision == pytest.approx(0.8)
    assert m.recall == pytest.approx(8 / 9)
    assert m.fp_rate == pytest.approx(2 / 11)
    assert m.fn_rate == pytest.approx(1 / 9)
    assert m.get("f1") == pytest.approx(2 * 0.8 * (8 / 9) / (0.8 + 8 / 9))


def test_metrics_zero_safe():
    m = Metrics.from_counts(tp=0, fp=0, fn=0, tn=0)
    assert m.precision == 0.0 and m.recall == 0.0 and m.f1 == 0.0


def test_paramdef_rejects_bad_key():
    with pytest.raises(ValidationError):
        ParamDef(key="NotDotted", dtype=Dtype.float, default=0.5, range=(0, 1), step=0.1, stage="s")


def test_paramdef_rejects_default_out_of_range():
    with pytest.raises(ValidationError):
        ParamDef(key="a.b", dtype=Dtype.float, default=2.0, range=(0, 1), step=0.1, stage="s")


def test_paramdef_clamp():
    p = ParamDef(key="a.b", dtype=Dtype.float, default=0.5, range=(0.0, 1.0), step=0.1, stage="s")
    assert p.clamp(1.5) == 1.0
    assert p.clamp(-1) == 0.0


def test_uuid7_format():
    i = new_id()
    assert len(i) == 36 and i[14] == "7"  # version nibble


def test_runresult_roundtrip():
    r = RunResult(run_id="r", status=RunStatus.evaluated, metrics=Metrics.from_counts(1, 0, 0, 1), n_items=2)
    again = RunResult(**r.model_dump())
    assert again.status is RunStatus.evaluated and again.metrics["tp"] == 1
    assert again.metrics["precision"] == 1.0  # Metrics models normalize to a {name: float} dict
