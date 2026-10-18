"""Structured run tracing: a black box for the whole pipeline.

Every scan writes phase records to `$TREND_RADAR_HOME/trace.jsonl`:
scan.start -> source.done* -> scan.fetch -> scan.dedupe -> scan.gate -> scan.done.
`tools/verify_run.py` replays one run and checks the invariants (counts add up,
scores are in range, no phase is missing, no error was swallowed).

Disabled only by TREND_RADAR_TRACE=0. Failures to write never affect a scan.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from .store import home

_LOCK = threading.Lock()
MAX_BYTES = 8_000_000


def enabled() -> bool:
    return os.environ.get("TREND_RADAR_TRACE", "1") != "0"


def record(event: str, **fields) -> None:
    if not enabled():
        return
    try:
        path = home() / "trace.jsonl"
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_name("trace.1.jsonl"))
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "pid": os.getpid(), "event": event, **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - tracing must never break a scan
        pass
