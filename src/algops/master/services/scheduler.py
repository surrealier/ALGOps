"""Scheduler — cron benchmarks and git-push detection (polling; no inbound webhook needed).

* ``cron`` schedules fire a benchmark of their scope at each cron tick (server local time).
* ``watch`` schedules (and every project created with ``watch: true``) poll the project's default
  ref every ``interval_s``; when the resolved sha moves, a benchmark of just the changed
  projects starts (trigger ``watch``) — so every new commit on the default branch is tested.

Run it in exactly ONE master process (``scheduler_enabled``); ``tick()`` is idempotent-ish via
``last_fired_at`` so a restart does not double-fire a cron tick.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from croniter import croniter

from algops.tracking import get_session, repo

from ..context import MasterContext
from .benchmarks import start_benchmark
from .projects import sync_project

DEFAULT_WATCH_INTERVAL_S = 300


def _local_now() -> datetime:
    return datetime.now().astimezone()


def next_cron(expr: str, after: datetime) -> datetime:
    base = after.astimezone() if after.tzinfo else after.replace(tzinfo=_local_now().tzinfo)
    return croniter(expr, base).get_next(datetime)


def _utc(dt: datetime) -> datetime:
    """Store UTC only — SQLite drops tzinfo, and naive values are read back as UTC."""
    return dt.astimezone(timezone.utc)


def valid_cron(expr: str) -> bool:
    return croniter.is_valid(expr)


class Scheduler:
    def __init__(self, ctx: MasterContext) -> None:
        self.ctx = ctx
        self._project_checked: dict[str, datetime] = {}
        self._running: set[str] = set()

    async def tick(self, now: datetime | None = None) -> list[str]:
        now = now or _local_now()
        started: list[str] = []
        with get_session() as s:
            schedules = [
                {"id": sc.schedule_id, "name": sc.name, "kind": sc.kind, "cron": sc.cron,
                 "interval_s": sc.interval_s, "scope": sc.scope, "enabled": sc.enabled,
                 "last": repo.aware(sc.last_fired_at), "created": repo.aware(sc.created_at),
                 "last_seen": dict(sc.last_seen or {})}
                for sc in repo.list_schedules(s)
            ]
            watched = [p.project_id for p in repo.list_projects(s) if p.watch]
        for sc in schedules:
            if not sc["enabled"]:
                continue
            if sc["kind"] == "cron" and sc["cron"]:
                bid = self._tick_cron(sc, now)
                if bid:
                    started.append(bid)
            elif sc["kind"] == "watch":
                bid = await self._tick_watch(sc, now)
                if bid:
                    started.append(bid)
        bid = await self._tick_project_watch(watched, now)
        if bid:
            started.append(bid)
        return started

    def _snapshot(self, schedule_id: str) -> dict | None:
        with get_session() as s:
            sc = repo.get_schedule(s, schedule_id)
            if sc is None:
                return None
            return {"id": sc.schedule_id, "name": sc.name, "kind": sc.kind, "cron": sc.cron,
                    "interval_s": sc.interval_s, "scope": sc.scope, "enabled": sc.enabled,
                    "last": repo.aware(sc.last_fired_at), "created": repo.aware(sc.created_at),
                    "last_seen": dict(sc.last_seen or {})}

    async def fire(self, schedule_id: str, now: datetime | None = None) -> dict:
        """Run a schedule *now* (cron: start its benchmark; watch: check for new commits)."""
        now = now or _local_now()
        sc = self._snapshot(schedule_id)
        if sc is None:
            return {"error": "schedule not found"}
        if sc["kind"] == "cron":
            bid = start_benchmark(self.ctx, name=f"{sc['name']} (수동 실행)", scope=sc["scope"],
                                  baseline="previous", trigger="schedule", schedule_id=sc["id"])
            with get_session() as s:
                row = repo.get_schedule(s, sc["id"])
                if row is not None:
                    row.last_benchmark_id = bid
            return {"benchmark_id": bid}
        sc["last"] = None  # bypass the interval
        bid = await self._tick_watch(sc, now)
        return {"benchmark_id": bid, "changed": bool(bid)}

    def _tick_cron(self, sc: dict, now: datetime) -> str | None:
        anchor = sc["last"] or sc["created"] or now
        try:
            due = next_cron(sc["cron"], anchor)
        except Exception:  # noqa: BLE001 — invalid expression: never fire
            return None
        if now < due:
            with get_session() as s:
                row = repo.get_schedule(s, sc["id"])
                if row is not None and repo.aware(row.next_fire_at) != due:
                    row.next_fire_at = _utc(due)
            return None
        bid = start_benchmark(self.ctx, name=f"{sc['name']} ({now:%Y-%m-%d %H:%M})", scope=sc["scope"],
                              baseline="previous", trigger="schedule", schedule_id=sc["id"])
        with get_session() as s:
            row = repo.get_schedule(s, sc["id"])
            if row is not None:
                row.last_fired_at = _utc(now)
                row.last_benchmark_id = bid
                row.next_fire_at = _utc(next_cron(sc["cron"], now))
        return bid

    async def _changed_projects(self, targets: list[str | tuple[str, str | None]],
                                last_seen: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
        """Sync each (project, ref) and report those whose resolved sha moved since ``last_seen``.

        ``last_seen`` is keyed by project id for the default ref and ``<project_id>@<ref>``
        for an explicit ref, so watching ``release`` never compares against ``main``.
        """
        changed: list[dict] = []
        seen = dict(last_seen)
        for target in targets:
            pid, ref = (target, None) if isinstance(target, str) else target
            key = pid if not ref else f"{pid}@{ref}"
            rev, _err = await sync_project(self.ctx, pid, ref)
            if rev is None:
                continue
            prev = seen.get(key)
            seen[key] = rev.sha
            if prev is not None and prev != rev.sha:
                changed.append({"project_id": pid, "ref": rev.sha})
        return changed, seen

    async def _tick_watch(self, sc: dict, now: datetime) -> str | None:
        anchor = sc["last"] or sc["created"] or now
        if sc["last"] is not None and now - anchor < timedelta(seconds=max(30, sc["interval_s"])):
            return None
        scope = sc["scope"] or []
        with get_session() as s:
            targets = ([(x["project_id"], x.get("ref") or None) for x in scope] if scope
                       else [(p.project_id, None) for p in repo.list_projects(s)])
        changed, seen = await self._changed_projects(targets, sc["last_seen"])
        bid = None
        if changed:
            filters = {x["project_id"]: {k: v for k, v in x.items() if k != "ref"} for x in scope}
            items = [{**filters.get(c["project_id"], {}), **c} for c in changed]
            bid = start_benchmark(self.ctx, name=f"{sc['name']} — 새 커밋", scope=items,
                                  baseline="previous", trigger="watch", schedule_id=sc["id"])
        with get_session() as s:
            row = repo.get_schedule(s, sc["id"])
            if row is not None:
                row.last_fired_at = _utc(now)
                row.last_seen = seen
                if bid:
                    row.last_benchmark_id = bid
        return bid

    async def _tick_project_watch(self, project_ids: list[str], now: datetime) -> str | None:
        interval = timedelta(seconds=DEFAULT_WATCH_INTERVAL_S)
        due = [pid for pid in project_ids if now - self._project_checked.get(pid, datetime.min.replace(tzinfo=now.tzinfo)) >= interval]
        if not due:
            return None
        for pid in due:
            self._project_checked[pid] = now
        with get_session() as s:
            last_seen = {pid: (repo.get_project(s, pid).last_seen_sha or "") for pid in due}
        last_seen = {k: v for k, v in last_seen.items() if v}
        changed, seen = await self._changed_projects(due, last_seen)
        with get_session() as s:
            for pid, sha in seen.items():
                proj = repo.get_project(s, pid)
                if proj is not None:
                    proj.last_seen_sha = sha
        if not changed:
            return None
        return start_benchmark(self.ctx, name="새 커밋 감지", scope=changed, baseline="previous", trigger="watch")

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the scheduler must never die
                print(f"[scheduler] tick error: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(self.ctx.settings.scheduler_tick_s)
