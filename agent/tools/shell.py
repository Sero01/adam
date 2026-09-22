"""shell tool: run a command in the workspace as the unprivileged agent user."""
from __future__ import annotations

import asyncio
import os
import signal
import sys

TAIL_BYTES = 4096


class _Tail:
    """Keeps only the last N bytes of a stream, so `yes` can't eat memory."""

    def __init__(self, n: int = TAIL_BYTES):
        self.n = n
        self.buf = bytearray()
        self.total = 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            self.total += len(chunk)
            self.buf += chunk
            if len(self.buf) > self.n:
                del self.buf[: len(self.buf) - self.n]

    def text(self) -> str:
        s = self.buf.decode("utf-8", errors="replace")
        if self.total > self.n:
            s = f"[... {self.total - self.n} earlier bytes omitted ...]\n" + s
        return s


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def clean_env(workspace: str) -> dict[str, str]:
    """Environment for agent commands. API keys are not passed through."""
    keep = {k: v for k, v in os.environ.items()
            if k in ("PATH", "LANG", "LC_ALL", "TZ", "TERM", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")}
    keep.update(HOME=workspace, USER="agent", PWD=workspace)
    return keep


async def run_shell(cmd: str, *, cwd: str, timeout: float, run_as: str | None = None,
                    env: dict[str, str] | None = None) -> dict:
    kwargs: dict = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True  # own process group -> kill the whole tree
        if run_as and os.geteuid() == 0:
            kwargs.update(user=run_as, group=run_as, extra_groups=[])
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    else:  # dev/test only
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = _Tail(), _Tail()
    readers = asyncio.gather(out.drain(proc.stdout), err.drain(proc.stderr))
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill(proc)
    finally:
        if proc.returncode is None:  # cancelled (STOP / watchdog) or timed out
            _kill(proc)
    try:
        await asyncio.wait_for(proc.wait(), 5)
        # background children may hold the pipes open; don't wait on them forever
        await asyncio.wait_for(readers, 2)
    except asyncio.TimeoutError:
        readers.cancel()
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "stdout": out.text(),
        "stderr": err.text(),
    }
