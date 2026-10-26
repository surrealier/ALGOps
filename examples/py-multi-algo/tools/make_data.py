"""Regenerate the example suites deterministically: ``python tools/make_data.py``."""

from __future__ import annotations

import csv
import math
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def detections(path: str, n: int, seed: int) -> None:
    """Detector candidates: real targets are confident and large; distractors are small/low."""
    rng = random.Random(seed)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "frame", "det", "score", "area", "label"])
        for i in range(n):
            label = 1 if rng.random() < 0.45 else 0
            if label:
                score = min(1.0, max(0.0, rng.gauss(0.68, 0.15)))
                area = max(4, int(rng.gauss(220, 90)))
            else:
                score = min(1.0, max(0.0, rng.gauss(0.42, 0.18)))
                area = max(4, int(rng.gauss(90, 70)))
            w.writerow([f"img{i // 4:04d}", i // 4, i % 4, f"{score:.4f}", area, label])


def signal(path: str, n: int, seed: int) -> None:
    """A noisy sine with impulsive outliers; the smoother should recover `truth`."""
    rng = random.Random(seed)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "observed", "truth"])
        for t in range(n):
            truth = math.sin(t / 15.0) + 0.3 * math.sin(t / 4.0)
            obs = truth + rng.gauss(0, 0.12)
            if rng.random() < 0.04:
                obs += rng.choice([-1, 1]) * rng.uniform(1.0, 2.0)
            w.writerow([t, f"{obs:.5f}", f"{truth:.5f}"])


if __name__ == "__main__":
    detections(os.path.join(ROOT, "data", "detector", "smoke", "items.csv"), 240, seed=7)
    detections(os.path.join(ROOT, "data", "detector", "full", "items.csv"), 1200, seed=11)
    signal(os.path.join(ROOT, "data", "smoother", "smoke", "signal.csv"), 300, seed=3)
    signal(os.path.join(ROOT, "data", "smoother", "full", "signal.csv"), 1500, seed=5)
    print("example data regenerated under", os.path.join(ROOT, "data"))
