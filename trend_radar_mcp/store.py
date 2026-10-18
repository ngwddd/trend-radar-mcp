"""SQLite state: what has this engine already seen, and what did each run emit?

The v1 skill had to *guess* whether an item was new, because nothing remembered
yesterday. Here the first sighting is a fact, so "新" stops being a subjective
call. This is the main capability a stateful MCP server has and a stateless
script cannot have.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    event_id   TEXT PRIMARY KEY,
    title      TEXT,
    url        TEXT,
    source     TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_items_last_seen ON items(last_seen);

CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    domain     TEXT,
    request    TEXT,
    stats      TEXT,
    path       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);

CREATE TABLE IF NOT EXISTS source_health (
    key          TEXT PRIMARY KEY,
    last_try     TEXT,
    last_ok      TEXT,
    empty_streak INTEGER NOT NULL DEFAULT 0,
    last_count   INTEGER NOT NULL DEFAULT 0
);
"""


def home() -> Path:
    """State directory. One env var, no surprises on Windows."""
    root = os.environ.get("TREND_RADAR_HOME")
    p = Path(root) if root else Path.home() / ".trend-radar"
    p.mkdir(parents=True, exist_ok=True)
    (p / "runs").mkdir(exist_ok=True)
    (p / "cache").mkdir(exist_ok=True)
    return p


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else home() / "state.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- items ---------------------------------------------------------
    def touch_items(self, entries: list[dict]) -> dict[str, dict]:
        """Record every event_id and return its history.

        Returns {event_id: {"first_seen": iso, "times_seen": n_before_this_run}}.
        A brand-new event gets times_seen=0, so callers can distinguish "never
        seen" from "seen once, last week".
        """
        out: dict[str, dict] = {}
        stamp = _now()
        with self._lock:
            for entry in entries:
                eid = entry.get("event_id")
                if not eid:
                    continue
                row = self._conn.execute(
                    "SELECT first_seen, times_seen FROM items WHERE event_id=?",
                    (eid,)).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO items(event_id,title,url,source,first_seen,last_seen,times_seen)"
                        " VALUES(?,?,?,?,?,?,1)",
                        (eid, (entry.get("title") or "")[:300], entry.get("url"),
                         entry.get("source_key") or entry.get("source_label"), stamp, stamp))
                    out[eid] = {"first_seen": stamp, "times_seen": 0}
                else:
                    self._conn.execute(
                        "UPDATE items SET last_seen=?, times_seen=times_seen+1 WHERE event_id=?",
                        (stamp, eid))
                    out[eid] = {"first_seen": row["first_seen"],
                                "times_seen": int(row["times_seen"])}
            self._conn.commit()
        return out

    def history(self, event_id: str | None = None, query: str | None = None,
                limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM items"
        args: tuple = ()
        if event_id:
            sql += " WHERE event_id=?"
            args = (event_id,)
        elif query:
            sql += " WHERE title LIKE ?"
            args = (f"%{query}%",)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        args = args + (max(1, min(int(limit), 100)),)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # -- runs ----------------------------------------------------------
    def start_run(self, domain: str, request: str) -> str:
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(run_id,created_at,domain,request,stats,path)"
                " VALUES(?,?,?,?,?,?)",
                (run_id, _now(), domain, request or "", "{}", ""))
            self._conn.commit()
        return run_id

    def finish_run(self, run_id: str, payload: dict, stats: dict) -> Path:
        """Persist the full run to disk and keep a metadata row in SQLite."""
        path = home() / "runs" / f"{run_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        with self._lock:
            self._conn.execute("UPDATE runs SET stats=?, path=? WHERE run_id=?",
                               (json.dumps(stats, ensure_ascii=False), str(path), run_id))
            self._conn.commit()
        return path

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        if row["path"] and Path(row["path"]).is_file():
            out["payload"] = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
        return out

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id,created_at,domain,request,stats FROM runs"
                " ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["stats"] = json.loads(d.get("stats") or "{}")
            except Exception:
                d["stats"] = {}
            out.append(d)
        return out

    # -- source health -------------------------------------------------
    def should_skip(self, key: str, hours: int = 12) -> bool:
        """Skip a source that returned 0 items on its last 3 tries.

        A dead/empty feed is not free: it still costs a request and a timeout
        slot. The streak resets on the first non-empty answer, so a feed that
        comes back is picked up again on the next run.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT empty_streak,last_try FROM source_health WHERE key=?",
                (key,)).fetchone()
        if row is None or int(row["empty_streak"]) < 3:
            return False
        try:
            last = datetime.fromisoformat(row["last_try"])
        except (TypeError, ValueError):
            return False
        return (datetime.now(timezone.utc) - last) < timedelta(hours=hours)

    def record_source(self, key: str, ok: bool, count: int) -> None:
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT empty_streak FROM source_health WHERE key=?", (key,)).fetchone()
            streak = int(row["empty_streak"]) if row else 0
            if ok and count > 0:
                streak = 0
            elif ok and count == 0:
                streak += 1
            self._conn.execute(
                "INSERT INTO source_health(key,last_try,last_ok,empty_streak,last_count)"
                " VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
                " last_try=excluded.last_try, last_ok=excluded.last_ok,"
                " empty_streak=excluded.empty_streak, last_count=excluded.last_count",
                (key, now, now if (ok and count > 0) else None, streak, count))
            self._conn.commit()

    def health(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM source_health ORDER BY empty_streak DESC, last_try DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            items = self._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            runs = self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        return {"items": items, "runs": runs, "db": str(self.path)}
