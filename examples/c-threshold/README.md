# Example: an algorithm in C

`env.setup` compiles `src/eval.c` once per commit; the entrypoint is the binary. Parameters
arrive as `ALGOPS_PARAM_FILTER_THRESHOLD` / `ALGOPS_PARAM_FILTER_MIN_STREAK`, the suite as
`ALGOPS_SUITE_PATH`, and the program writes `$ALGOPS_RESULT`. Nothing about this repository
knows ALGOps exists beyond `algops.yaml`.

```bash
algops test examples/c-threshold
algops run  examples/c-threshold -s full --set filter.min_streak=3
```
