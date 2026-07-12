"""Minimal leveled logger; one line per event, callId-scoped where available."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

_ORDER = {"debug": 10, "info": 20, "warn": 30, "error": 40}

# Fall back to "info" for an unset OR invalid LOG_LEVEL. Without the membership
# check, a typo (e.g. LOG_LEVEL=verbose) would otherwise emit every level.
_requested = os.environ.get("LOG_LEVEL", "").strip().lower()
_MIN_LEVEL = _requested if _requested in _ORDER else "info"


def _emit(level: str, scope: str, msg: str, extra: object = None) -> None:
    if _ORDER[level] < _ORDER[_MIN_LEVEL]:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    tail = "" if extra is None else f" {json.dumps(extra, default=str)}"
    line = f"{ts} {level.upper():<5} [{scope}] {msg}{tail}"
    print(line, file=sys.stderr if level == "error" else sys.stdout, flush=True)


class Logger:
    """Scope-bound logger with the four conventional levels."""

    __slots__ = ("scope",)

    def __init__(self, scope: str) -> None:
        self.scope = scope

    def debug(self, msg: str, extra: object = None) -> None:
        _emit("debug", self.scope, msg, extra)

    def info(self, msg: str, extra: object = None) -> None:
        _emit("info", self.scope, msg, extra)

    def warn(self, msg: str, extra: object = None) -> None:
        _emit("warn", self.scope, msg, extra)

    def error(self, msg: str, extra: object = None) -> None:
        _emit("error", self.scope, msg, extra)


def logger(scope: str) -> Logger:
    return Logger(scope)
