"""Deterministic multi-parameter search strategies (the campaign's non-LLM proposer).

All strategies are pure: ``propose(...)`` takes the evaluated history and a JSON-serializable
``state`` (kept in the LangGraph checkpoint) and returns up to ``k`` new candidate parameter
sets, the new state, and whether the space is exhausted ("converged"). Already-evaluated
parameter sets are never proposed again.

* ``coordinate`` — compass/pattern search around the incumbent best: polls ±step on each key
  (other choices for enum/bool), follows a successful move (momentum), halves steps when a full
  poll fails, converges when every step is at its floor. Batch-friendly (k poll points at once).
* ``random``     — seeded uniform sampling of each key's grid.
* ``grid``       — exhaustive cartesian product of the keys' grids (capped), in order.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
from typing import Any

from algops.core import Dtype, GoalDirection, Objective, ParamSpec

STRATEGIES = ("coordinate", "random", "grid")
_GRID_CAP = 4096
_INCREASE_FIRST = {"fp", "fp_rate", "precision", "fpr"}
_DECREASE_FIRST = {"fn", "fn_rate", "recall", "fnr", "tpr"}


def params_key(params: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def best_entry(history: list[dict], objective: Objective) -> dict | None:
    best = None
    for h in history:
        if h.get("status", "evaluated") != "evaluated" or not h.get("metrics"):
            continue
        if best is None or objective.rank(h["metrics"]) < objective.rank(best["metrics"]):
            best = h
    return best


def _preferred_dir(objective: Objective) -> int:
    if objective.metric in _INCREASE_FIRST:
        return 1
    if objective.metric in _DECREASE_FIRST:
        return -1
    return 1 if objective.direction is GoalDirection.minimize else -1


def _seen(history: list[dict]) -> set[str]:
    return {params_key(h["params"]) for h in history if "params" in h}


def propose(
    strategy: str,
    spec: ParamSpec,
    keys: list[str],
    objective: Objective,
    history: list[dict],
    state: dict | None,
    k: int,
    *,
    seed: int = 0,
    base: dict[str, Any] | None = None,
) -> tuple[list[dict], dict, bool]:
    state = dict(state or {})
    base = dict(base or spec.effective())
    keys = [key for key in keys if key in spec.by_key()]
    if not keys or k <= 0:
        return [], state, True
    if strategy == "random":
        return _random(spec, keys, history, state, k, seed, base)
    if strategy == "grid":
        return _grid(spec, keys, history, state, k, base)
    return _coordinate(spec, keys, objective, history, state, k, base)


# --------------------------------------------------------------------------- coordinate


def _coordinate(spec, keys, objective, history, state, k, base):
    by_key = spec.by_key()
    steps: dict[str, float] = dict(state.get("steps") or {})
    for key in keys:
        p = by_key[key]
        if key not in steps and p.dtype in (Dtype.float, Dtype.int):
            steps[key] = float(p.step or 0.0)
    pref = int(state.get("pref_dir") or _preferred_dir(objective))
    moves: dict[str, list] = dict(state.get("moves") or {})  # params_key -> [key, dir]
    best = best_entry(history, objective)
    center = dict(best["params"]) if best else dict(base)
    center_hash = params_key(center)
    last_move = state.get("last_move")
    if center_hash != state.get("center") and center_hash in moves:
        last_move = moves[center_hash]  # the move that produced the new incumbent
    seen = _seen(history)

    def floor(key: str) -> float:
        return float(by_key[key].step or 0.0) * 0.125

    def poll_points() -> list[tuple[dict, list]]:
        pts: list[tuple[dict, list]] = []
        order = list(keys)
        if last_move and last_move[0] in order:  # momentum: retry the winning move first
            order.remove(last_move[0])
            order.insert(0, last_move[0])
        for key in order:
            p = by_key[key]
            if p.dtype in (Dtype.float, Dtype.int):
                first = int(last_move[1]) if (last_move and last_move[0] == key) else pref
                for d in (first, -first):
                    cand = dict(center)
                    cand[key] = p.coerce(float(center[key]) + d * steps[key])
                    if cand[key] != center[key]:
                        pts.append((cand, [key, d]))
            else:
                for choice in p.grid():
                    if choice != center[key]:
                        cand = dict(center)
                        cand[key] = choice
                        pts.append((cand, [key, 0]))
        return pts

    out: list[dict] = []
    out_hashes: set[str] = set()
    converged = False
    for _ in range(16):  # step-halving rounds
        for cand, move in poll_points():
            h = params_key(cand)
            if h in seen or h in out_hashes:
                continue
            out.append(cand)
            out_hashes.add(h)
            moves[h] = move
            if len(out) >= k:
                break
        if out:
            break
        shrunk = False
        for key in list(steps):
            if steps[key] > floor(key) + 1e-12:
                steps[key] = max(steps[key] * 0.5, floor(key))
                shrunk = True
        if not shrunk:
            converged = True
            break
    new_state = {
        "steps": steps, "pref_dir": pref, "moves": _trim(moves), "center": center_hash,
        "last_move": last_move,
    }
    return out, new_state, converged and not out


def _trim(moves: dict, keep: int = 512) -> dict:
    if len(moves) <= keep:
        return moves
    return dict(list(moves.items())[-keep:])


# --------------------------------------------------------------------------- random


_ENUMERATE_LIMIT = 4096  # sub-spaces up to this size are sampled exactly (without replacement)


def _random(spec, keys, history, state, k, seed, base):
    by_key = spec.by_key()
    grids = {key: by_key[key].grid() for key in keys}
    space = 1
    for g in grids.values():
        space *= max(1, len(g))
    draws = int(state.get("draws") or 0)
    rng = random.Random(f"{seed}:{draws}")
    seen = _seen(history)
    out: list[dict] = []
    if space <= _ENUMERATE_LIMIT:
        # Small space: list what is left of *this* sub-space (other keys at `base`) and sample it.
        # Points evaluated elsewhere (baseline with other keys, LLM proposals) never count.
        remaining = []
        for combo in itertools.product(*(grids[key] for key in keys)):
            cand = dict(base)
            cand.update(zip(keys, combo))
            if params_key(cand) not in seen:
                remaining.append(cand)
        rng.shuffle(remaining)
        out = remaining[:k]
        return out, {"draws": draws + 1}, not remaining
    hashes: set[str] = set()
    attempts = 0
    while len(out) < k and attempts < 50 * k:
        attempts += 1
        cand = dict(base)
        for key in keys:
            cand[key] = rng.choice(grids[key])
        h = params_key(cand)
        if h in seen or h in hashes:
            continue
        out.append(cand)
        hashes.add(h)
    return out, {"draws": draws + 1}, False  # a huge space is never declared exhausted


# --------------------------------------------------------------------------- grid


def _grid(spec, keys, history, state, k, base):
    by_key = spec.by_key()
    per_key = max(2, int(_GRID_CAP ** (1.0 / max(1, len(keys)))))
    grids = [by_key[key].grid(max_points=per_key) for key in keys]
    cursor = int(state.get("cursor") or 0)
    seen = _seen(history)
    out: list[dict] = []
    total = 1
    for g in grids:
        total *= len(g)
    it = itertools.islice(itertools.product(*grids), cursor, None)
    for combo in it:
        cursor += 1
        cand = dict(base)
        cand.update(dict(zip(keys, combo)))
        if params_key(cand) in seen:
            continue
        out.append(cand)
        if len(out) >= k:
            break
    converged = cursor >= min(total, _GRID_CAP) and not out
    return out, {"cursor": cursor}, converged
