#!/usr/bin/env python3
"""Launch a long-running command fully detached from this shell.

macOS has no ``setsid``, and a plain ``nohup ... &`` stays in the caller's
process group — so anything that signals that group (an interrupted shell,
a cancelled tool call) also kills the engine mid-session. This double-forks and
calls ``os.setsid()`` so the child owns a new session and survives.

Usage:
    uv run python scripts/spawn_detached.py --log session1/logs/stdout.log \
        --pidfile session1/session.pid -- .venv/bin/polymaker run --paper ...
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--pidfile", type=Path)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()

    cmd = [c for c in args.cmd if c != "--"]
    if not cmd:
        raise SystemExit("no command given (put it after --)")

    args.log.parent.mkdir(parents=True, exist_ok=True)

    # first fork: parent returns to the shell immediately
    if os.fork() > 0:
        return 0
    os.setsid()                      # new session, no controlling terminal
    # second fork: the grandchild can never reacquire a terminal
    if os.fork() > 0:
        os._exit(0)

    fd = os.open(str(args.log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    if args.pidfile:
        args.pidfile.write_text(str(os.getpid()))
    try:
        os.execv(cmd[0], cmd)
    except OSError as exc:           # pragma: no cover
        sys.stderr.write(f"exec failed: {exc}\n")
        os._exit(127)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
