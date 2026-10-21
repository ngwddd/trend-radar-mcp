"""Potential-news capability branch.

This module is deliberately isolated from `scan.py` / `scoring.py`:

* it never changes the default `radar_scan` response;
* it adds no table and no write path of its own — velocity comes from the
  `runs/*.json` payloads that `scan()` already persists;
* every failure is contained here and degrades to a tier/freshness shortlist.

The server only does the arithmetic a model cannot do (it cannot see the
stream history). Semantic judgement — salience, novelty, "will this break
out" — is left to the model reading the shortlist.
"""

from __future__ import annotations

import math
import time
from datetime import datetime

from .registry import SOURCE_WEIGHT, identity_of

MAX_TOP = 12
DEFAULT_TOP = 8
REUSE_SECONDS = 300
ENRICH_DEADLINE = 1.5


def _now() -> datetime:
    return datetime.now()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d


def _weight(name: str | None) -> float:
    if not name:
        return 0.0
    w = SOURCE_WEIGHT.get(name)
    if w is None:
        w = SOURCE_WEIGHT.get(identity_of(name))
    return float(w or 0.0)


def _sources(entry: dict) -> set[str]:
    out: set[str] = set()
    key = entry.get("source_key")
    if key:
        out.add(identity_of(key))
    for x in entry.get("also_seen_in") or []:
        if x:
            out.add(identity_of(x))
    return out


def _tier(entry: dict) -> float:
    weights = [_weight(name) for name in _sources(entry)]
    weights = [w for w in weights if w > 0]
    return max(weights) if weights else 0.45


def _eligible(entry: dict) -> bool:
    if not entry.get("url") or not entry.get("title"):
        return False
    if entry.get("bucket") == "calibration":      # trend boards are signal, not story
        return False
    if entry.get("promotional_signal") or entry.get("customer_story"):
        return False
    return True


def _age_hours(entry: dict, now: datetime) -> float:
    first = _parse(entry.get("first_seen") or entry.get("published"))
    if first is None:
        return 12.0
    return max(0.0, (now - first).total_seconds() / 3600.0)


def _previous_index(payloads: list[dict]) -> tuple[dict[str, set], dict[str, list[int]], datetime | None]:
    """Union of sources per event + per-run source counts + oldest generated_at."""
    union: dict[str, set] = {}
    counts: dict[str, list[int]] = {}
    oldest: datetime | None = None
    for p in payloads:
        ts = _parse(p.get("generated_at"))
        if ts and (oldest is None or ts < oldest):
            oldest = ts
        for e in p.get("entries") or []:
            eid = e.get("event_id")
            if not eid:
                continue
            union.setdefault(eid, set()).update(_sources(e))
            counts.setdefault(eid, []).append(len(_sources(e)))
    return union, counts, oldest


def _freshness(age_hours: float) -> float:
    return max(0.0, min(1.0, 1.0 - age_hours / 24.0))


def _score(entry: dict, prev_union: dict[str, set], prev_counts: dict[str, list[int]],
           oldest: datetime | None, now: datetime) -> dict:
    eid = entry.get("event_id") or ""
    cur = _sources(entry)
    prior = prev_union.get(eid) or set()
    new = sorted(cur - prior)
    tier = _tier(entry)
    age = _age_hours(entry, now)
    has_history = bool(prev_counts.get(eid))

    signals: dict = {
        "new_sources": len(new),
        "new_source_names": new[:6],
        "tier": round(tier, 2),
        "age_hours": round(age, 1),
        "historical_sources": len(prior),
    }
    why: list[str] = []

    if has_history or prev_union:
        span = 1.0
        if oldest is not None:
            span = max(0.25, min(6.0, (now - oldest).total_seconds() / 3600.0))
        velocity = len(new) / span
        counts = prev_counts.get(eid) or []
        avg = sum(counts) / len(counts) if counts else 0.0
        burst = (len(cur) + 1.0) / (avg + 1.0)
        burst = max(1.0, min(4.0, burst))
        vel_norm = max(0.0, min(1.0, velocity / 3.0))
        burst_norm = math.log1p(burst) / math.log(5.0)
        score = 100.0 * (0.50 * vel_norm + 0.30 * burst_norm + 0.20 * tier)
        signals.update({
            "velocity_per_hour": round(velocity, 2),
            "burst": round(burst, 2),
            "history_runs": len(counts),
        })
        if new:
            why.append(f"{len(new)} 个新源 / {span:.1f}h")
        if burst >= 1.2:
            why.append(f"突发 {burst:.1f}×")
        why.append(f"早期源权重 {tier:.2f}")
        degraded: list[str] = []
    else:
        score = min(60.0, 100.0 * (0.65 * tier + 0.35 * _freshness(age)))
        signals.update({"velocity_per_hour": None, "burst": None, "history_runs": 0})
        why.append(f"首次出现 {age:.1f}h 前")
        why.append(f"源权重 {tier:.2f}")
        degraded = ["no_history"]

    if age <= 2:
        why.append("2h 内新条目")
    return {
        "event_id": eid,
        "title": entry.get("title"),
        "url": entry.get("url"),
        "source": entry.get("source"),
        "date": entry.get("published") or entry.get("first_seen"),
        "bucket": entry.get("bucket"),
        "score": int(round(score)),
        "signals": signals,
        "why": why[:3],
        "_degraded": degraded,
    }


def build_candidates(payload: dict, previous: list[dict] | None = None,
                     top: int = DEFAULT_TOP) -> dict:
    """Rank the current payload into a shortlist. Pure function, no I/O."""
    now = _now()
    previous = previous or []
    prev_union, prev_counts, oldest = _previous_index(previous)

    entries = [e for e in payload.get("entries") or [] if _eligible(e)]
    # General outlets are tagged with several domains; in a narrow AI/science
    # scan they can still emit a non-AI story. Only keep items that matched the
    # scope lexicon, and only when enough of them exist.
    scope = payload.get("domain") or payload.get("scope") or ""
    if scope in ("ai", "science"):
        focused = [e for e in entries if (e.get("topic_hits") or 0) > 0]
        if len(focused) >= max(3, min(int(top), MAX_TOP)):
            entries = focused

    rows = []
    for entry in entries:
        rows.append(_score(entry, prev_union, prev_counts, oldest, now))

    rows.sort(key=lambda r: (-r["score"], -(r["signals"].get("velocity_per_hour") or 0)))
    picked: list[dict] = []
    per_source: dict[str, int] = {}
    for row in rows:
        key = row.get("source") or ""
        if per_source.get(key, 0) >= 2:
            continue
        per_source[key] = per_source.get(key, 0) + 1
        picked.append(row)
        if len(picked) >= max(1, min(int(top), MAX_TOP)):
            break

    degraded = sorted({x for row in picked for x in row.pop("_degraded", [])})
    return {
        "candidates": picked,
        "meta": {
            "mode": "potential",
            "run_id": payload.get("run_id"),
            "reused_run": False,
            "lookback_runs": len(previous),
            "degraded": degraded,
            "generated_at": now.isoformat(timespec="seconds"),
            "caveat": "潜力（未验证）：按历史新增源与源层级预排，不代表已经传播开",
        },
    }


def fallback_candidates(payload: dict | None, top: int, reason: str) -> dict:
    """Never raise: return a tier/freshness shortlist with a degraded flag."""
    if not payload:
        return {"candidates": [],
                "meta": {"mode": "potential", "degraded": [reason],
                         "lookback_runs": 0, "reused_run": False,
                         "caveat": "潜力（未验证）"}}
    now = _now()
    rows = []
    for entry in payload.get("entries") or []:
        if not _eligible(entry):
            continue
        tier = _tier(entry)
        age = _age_hours(entry, now)
        rows.append({
            "event_id": entry.get("event_id"),
            "title": entry.get("title"),
            "url": entry.get("url"),
            "source": entry.get("source"),
            "date": entry.get("published") or entry.get("first_seen"),
            "bucket": entry.get("bucket"),
            "score": int(round(100 * (0.65 * tier + 0.35 * _freshness(age)))),
            "signals": {"tier": round(tier, 2), "age_hours": round(age, 1)},
            "why": [f"源权重 {tier:.2f}", f"首次出现 {age:.1f}h 前"],
        })
    rows.sort(key=lambda r: -r["score"])
    meta = {
        "mode": "potential",
        "run_id": payload.get("run_id"),
        "reused_run": False,
        "lookback_runs": 0,
        "degraded": [reason],
        "generated_at": now.isoformat(timespec="seconds"),
        "caveat": "潜力（未验证）：按源层级与新鲜度降级排序",
    }
    return {"candidates": rows[:max(1, min(int(top), MAX_TOP))], "meta": meta}


def age_seconds(payload: dict) -> float | None:
    ts = _parse(payload.get("generated_at"))
    if ts is None:
        return None
    return (_now() - ts).total_seconds()


def over_deadline(started: float) -> bool:
    return (time.monotonic() - started) > ENRICH_DEADLINE
