# Example: one repository, two algorithms, four suites

A minimal contract-v2 repository used by ALGOps' own tests and CI.

| algorithm | what it tunes | metrics | suites |
| --- | --- | --- | --- |
| `detector` | score threshold, min area, small-box rescue rule | binary preset (`fp_rate`, `recall`, …) + `latency_us` | `det-smoke`, `det-full` |
| `smoother` | EMA alpha, median window (inline params) | regression preset (`mae`, `rmse`, …) | `sig-smoke`, `sig-full` |

```bash
algops validate examples/py-multi-algo
algops run  examples/py-multi-algo -a detector -s det-smoke --set detector.score_threshold=0.6
algops test examples/py-multi-algo --json /tmp/base.json     # all 4 cells + gates
algops test examples/py-multi-algo --baseline /tmp/base.json # regression check vs a report
```

`python tools/make_data.py` regenerates the deterministic suite data.
