"""Subprocess execution with a hard timeout, whole-tree kill, and bounded log capture.

Used for every external program the runner starts (git, env setup, algorithm entrypoints).
stdout and stderr are merged and drained continuously (no pipe deadlock); only the tail is kept
so a chatty program cannot exhaust memory.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

LOG_LIMIT_BYTES = 2 * 1024 * 1024


@dataclass
class ProcResult:
    returncode: int | None  # None => killed on timeout
    output: str
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 — already gone
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


async def run_process(
    argv: list[str] | str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    shell: bool = False,
    log_limit: int = LOG_LIMIT_BYTES,
) -> ProcResult:
    """Run ``argv`` (or a shell string with ``shell=True``) and capture merged output."""
    kwargs: dict = {
        "cwd": cwd,
        "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
        "stdin": asyncio.subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True  # own process group => killpg on timeout

    started = time.monotonic()
    if shell:
        proc = await asyncio.create_subprocess_shell(argv if isinstance(argv, str) else " ".join(argv), **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)

    buf = bytearray()
    truncated = False

    async def _drain() -> None:
        nonlocal truncated
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > log_limit:
                del buf[: len(buf) - log_limit]
                truncated = True

    drain = asyncio.create_task(_drain())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            pass
    except asyncio.CancelledError:
        _kill_tree(proc)
        raise
    try:
        await asyncio.wait_for(drain, 10)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        drain.cancel()

    text = buf.decode("utf-8", errors="replace")
    if truncated:
        text = f"[... output truncated to last {log_limit} bytes ...]\n" + text
    return ProcResult(
        returncode=None if timed_out else proc.returncode,
        output=text,
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
    )
