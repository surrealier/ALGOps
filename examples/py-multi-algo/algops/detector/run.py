"""Detector post-processing entrypoint (contract v2, Python SDK — no ALGOps install needed)."""

import csv
import os
import time

import algops_sdk as sdk

RESCUE_SCORE = 0.85


def run(ctx: sdk.Context):
    thr = float(ctx.params["detector.score_threshold"])
    min_area = int(ctx.params["detector.min_area"])
    rule = ctx.params["detector.area_rule"]

    ai = ctx.stage("ai_inference", ["ai_score", "gt_match"])
    shape = ctx.stage("postproc_shape", ["feat_area"], depends_on=["ai_inference"])
    decision = ctx.stage("decision", ["dec_kept", "dec_reason", "dec_score", "gt_match"],
                         depends_on=["ai_inference", "postproc_shape"])
    tp = fp = fn = tn = 0
    started = time.perf_counter()
    with open(os.path.join(ctx.suite_path, "items.csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        score, area, gt = float(r["score"]), int(r["area"]), int(r["label"])
        eid = f"{r['item_id']}_d{r['det']}"
        ai.row(r["item_id"], int(r["frame"]), eid, ai_score=score, gt_match=gt)
        shape.row(r["item_id"], int(r["frame"]), eid, feat_area=area)
        if score < thr:
            kept, reason = False, "low_score"
        elif area < min_area and not (rule == "rescue_confident" and score >= RESCUE_SCORE):
            kept, reason = False, "small_area"
        else:
            kept, reason = True, "kept"
        decision.row(r["item_id"], int(r["frame"]), eid, dec_kept=kept, dec_reason=reason,
                     dec_score=score, gt_match=gt)
        if kept and gt:
            tp += 1
        elif kept:
            fp += 1
        elif gt:
            fn += 1
        else:
            tn += 1
    latency_us = (time.perf_counter() - started) * 1e6 / max(1, len(rows))
    return sdk.counts(tp=tp, fp=fp, fn=fn, tn=tn, latency_us=latency_us)
