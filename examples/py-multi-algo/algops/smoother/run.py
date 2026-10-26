"""Signal smoother entrypoint: moving median (outliers) + EMA (noise). Regression metrics."""

import csv
import math
import os

import algops_sdk as sdk


def _median_filter(xs, window):
    if window <= 1:
        return list(xs)
    half = window // 2
    out = []
    for i in range(len(xs)):
        lo, hi = max(0, i - half), min(len(xs), i + half + 1)
        seg = sorted(xs[lo:hi])
        out.append(seg[len(seg) // 2])
    return out


def run(ctx: sdk.Context):
    alpha = float(ctx.params["smoother.alpha"])
    window = int(ctx.params["smoother.window"])
    with open(os.path.join(ctx.suite_path, "signal.csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    obs = [float(r["observed"]) for r in rows]
    truth = [float(r["truth"]) for r in rows]
    pre = _median_filter(obs, window)
    est, prev = [], None
    for x in pre:
        prev = x if prev is None else alpha * x + (1 - alpha) * prev
        est.append(prev)
    errs = [e - t for e, t in zip(est, truth)]
    out = ctx.stage("smoothing", ["in_observed", "feat_prefiltered", "dec_estimate", "gt_truth"])
    for i, r in enumerate(rows):
        out.row("signal", i, f"t{i:05d}", in_observed=obs[i], feat_prefiltered=pre[i],
                dec_estimate=est[i], gt_truth=truth[i])
    mse = sum(e * e for e in errs) / len(errs)
    return sdk.result(
        metrics={
            "mae": sum(abs(e) for e in errs) / len(errs),
            "mse": mse,
            "rmse": math.sqrt(mse),
            "max_abs_error": max(abs(e) for e in errs),
        },
        n_items=len(rows),
    )
