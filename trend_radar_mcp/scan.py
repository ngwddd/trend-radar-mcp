"""The scan pipeline: concurrent collection -> scoring -> buckets -> state.

What is new versus v1 (the script this engine grew out of):

* every source is fetched CONCURRENTLY, with a per-host politeness limiter;
* fetch results are cached on disk (TTL), so a repeated scan is nearly free;
* every event gets a first_seen timestamp in SQLite, so "is this new?" is a
  fact rather than a judgement call;
* a run writes one file and returns a SMALL summary; the model asks for the
  entries it actually needs instead of reading a 320 KB blob.

The scoring/gating rules are v1's, ported verbatim: they encode failures that
were observed in real runs, and are not to be "simplified" casually.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlparse

from . import fetch as F
from .dedupe import event_id, is_same_event, title_tokens
from .relevance import (classify_request, effective_keyword_count,
                        intent_keywords, keyword_coverage)
from .registry import (CREDIBILITY_THRESHOLD, DOMAINS, ERRATA_DROP, ERRATA_RE,
                       HARD_EVIDENCE_TYPES, SOURCES, evidence_type_for, identity_of)
from .scoring import (ARCHIVE_SCAN_LIMIT, ARCHIVE_SOURCES, PROMO_BODY_PENALTY,
                      PROMO_PENALTY, apply_trend_cap, body_missing, comm_weight,
                      comm_weight_social, domain_of_source, domain_share,
                      estimate_pickups, external_truth_weight,
                      is_customer_story, notice_severity, platform_of,
                      promo_position, signal_words, trend_saturation,
                      truth_weight, truth_weight_social)
from .store import Store, home

STREAM_GAP = {"export.arxiv.org": 3.0, "api.crossref.org": 0.35}
DEFAULT_GAP = 0.05

BUCKET_OF_SECTION = {
    "核心区": "core",
    "待观察区": "watch",
    "存疑区": "suspect",
    "校准信号": "calibration",
    "丢弃": "discarded",
}


class HostLimiter:
    """Serialise requests to one host by a minimum interval.

    arXiv enforces ~3 s between API calls (v1 measured 429s without it), and
    Crossref is happier with a small gap when ten journals are requested at
    once. Different hosts never block each other.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    def _lock_for(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def wait(self, host: str, gap: float) -> None:
        if gap <= 0:
            return
        with self._lock_for(host):
            now = time.monotonic()
            delta = self._last.get(host, 0.0) + gap - now
            if delta > 0:
                time.sleep(delta)
            self._last[host] = time.monotonic()


def _host_of(spec: dict) -> str:
    url = spec.get("url") or (spec.get("feeds") or [""])[0]
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def resolve_keys(domain: str, streams: list[str] | None) -> list[str]:
    """Pick the sources for one domain and the requested streams.

    Trend boards are added to every full-pipeline run (they are the third,
    independent signal), but never to a single-stream request.
    """
    if domain not in DOMAINS:
        raise ValueError(f"unknown domain {domain!r}; valid: {list(DOMAINS)}")
    keys = [k for k, v in SOURCES.items() if domain in v.get("domains", [])]
    if streams:
        wanted = set(streams)
        keys = [k for k in keys if SOURCES[k].get("class") in wanted]
        if "trend" in wanted:
            keys += [k for k, v in SOURCES.items() if v.get("class") == "trend"]
    else:
        keys += [k for k, v in SOURCES.items() if v.get("class") == "trend"]
    return list(dict.fromkeys(keys))


def _source_cache_path(key: str, since: datetime, days: int, major_days: int,
                       max_per_source: int) -> pathlib.Path:
    raw = f"{key}|{since.date()}|{days}|{major_days}|{max_per_source}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return home() / "cache" / f"src-{digest}.json"


def _write_source_cache(out: dict, path: pathlib.Path) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _fetch_one(key: str, since: datetime, days: int, major_days: int,
               max_per_source: int, limiter: HostLimiter,
               force: bool = False) -> dict:
    """Fetch one source. Returns entries + a report row, never raises."""
    spec = SOURCES[key]
    cache_path = _source_cache_path(key, since, days, major_days, max_per_source)
    if not force:
        try:
            if time.time() - cache_path.stat().st_mtime < 300:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return {**cached, "seconds": 0.0, "cached": True}
        except (OSError, ValueError):
            pass
    timeout = spec.get("timeout", F.TIMEOUT)
    started = time.perf_counter()
    host = _host_of(spec)
    limiter.wait(host, STREAM_GAP.get(host, DEFAULT_GAP))
    out = {"key": key, "entries": [], "status": "ok", "detail": None,
           "seconds": 0.0, "archive_truncated": 0, "sub_failed": [],
           "stale": None}
    try:
        if spec["kind"] == "rss":
            got = F.fetch_rss(spec["url"], spec["label"], spec.get("ua"),
                              spec.get("link_prefer", "alternate"), timeout)
        elif spec["kind"] == "atom":
            got = F.fetch_atom_arxiv(spec, max_per_source)
        elif spec["kind"] == "crossref":
            got = F.fetch_crossref(
                spec, max_per_source,
                since - timedelta(days=max(0, major_days - days)))
        elif spec["kind"] == "rss_multi":
            got, sub_failed = F.fetch_rss_multi(
                spec["feeds"], spec["label"], spec.get("ua"),
                spec.get("link_prefer", "alternate"), timeout)
            out["sub_failed"] = sub_failed
        elif spec["kind"] == "toutiao_hot":
            got = F.fetch_toutiao_hot(spec, max_per_source)
        elif spec["kind"] == "google_trends":
            got = F.fetch_google_trends(spec, max_per_source)
        else:
            raise ValueError(f"unknown kind {spec['kind']!r}")

        for entry in got:
            entry["source_key"] = key
            entry["source_status"] = spec["status"]

        if key in ARCHIVE_SOURCES and len(got) > ARCHIVE_SCAN_LIMIT:
            got.sort(key=lambda e: e.get("published") or "", reverse=True)
            out["archive_truncated"] = len(got) - ARCHIVE_SCAN_LIMIT
            got = got[:ARCHIVE_SCAN_LIMIT]

        # A feed can answer HTTP 200 with year-old XML. That is worse than a
        # 404, so a source whose newest item predates the window is dropped
        # whole and reported. Date granularity on purpose: a datetime compare
        # made the window's first day fail against the same day.
        if spec.get("class") != "trend":
            dated = [d for d in (F._parse_date(e.get("published")) for e in got) if d]
            if dated and max(dated).date() < since.date():
                newest = max(dated).date().isoformat()
                out.update(status="stale-feed", detail=(
                    f"newest item {newest} is before window start {since.date().isoformat()}; "
                    "whole source excluded"), entries=[])
                out["stale"] = {"source": key, "label": spec["label"],
                                "newest_item": newest,
                                "window_start": since.date().isoformat()}
                out["seconds"] = round(time.perf_counter() - started, 2)
                _write_source_cache(out, cache_path)
                return out
        out["entries"] = got
    except Exception as exc:  # noqa: BLE001 - one source must not kill the run
        detail = str(exc)[:200]
        low = detail.lower()
        if "timed out" in low:
            detail = "timeout"
        elif "<urlopen error" in detail:
            detail = detail.split("<urlopen error", 1)[1].split(">")[0]
        out.update(status="failed", detail=detail)
    finally:
        out["seconds"] = round(time.perf_counter() - started, 2)
    if out["status"] in ("ok", "stale-feed"):
        _write_source_cache(out, cache_path)
    return out


def _compact_entry(e: dict, summary_chars: int = 400) -> dict:
    summary = e.get("summary") or ""
    if len(summary) > summary_chars:
        summary = summary[:summary_chars] + "…"
    return {
        "event_id": e.get("event_id"),
        "title": e.get("title"),
        "url": e.get("url"),
        "source": e.get("source_label"),
        "source_key": e.get("source_key"),
        "published": e.get("published"),
        "summary": summary,
        "summary_truncated": bool(e.get("summary_truncated")),
        "evidence_type": e.get("evidence_type"),
        "section": e.get("section"),
        "bucket": BUCKET_OF_SECTION.get(e.get("section") or "", "discarded"),
        "truth_score": e.get("truth_score"),
        "comm_score": e.get("comm_score"),
        "pickups": e.get("pickups"),
        "corroborated": e.get("corroborated"),
        "single_source_hard_evidence": e.get("single_source_hard_evidence"),
        "gate_note": e.get("gate_note"),
        "record_alert": e.get("record_alert"),
        "promotional_signal": e.get("promotional_signal"),
        "customer_story": e.get("customer_story"),
        "signal_words": e.get("signal_words") or [],
        "platform_key": e.get("platform_key"),
        "evidence_form": e.get("evidence_form"),
        "in_window": e.get("in_window"),
        "major_window": e.get("major_window"),
        "first_seen": e.get("first_seen"),
        "times_seen": e.get("times_seen"),
        "is_new": e.get("is_new"),
        "also_seen_in": e.get("also_seen_in") or [],
    }


def scan(domain: str, streams: list[str] | None = None, days: int = 3,
         major_days: int = 0, max_per_source: int = 30,
         request: str = "", keywords: list[str] | None = None,
         external: list[dict] | None = None, force_refresh: bool = False,
         max_workers: int = 12, store: Store | None = None) -> dict:
    """Run one scan and return a compact summary plus the run file path."""
    if not (1 <= int(days) <= 30):
        raise ValueError("days must be between 1 and 30")
    own_store = store is None
    store = store or Store()
    try:
        return _scan(domain, streams, int(days), int(major_days), int(max_per_source),
                     request or "", list(keywords or []), list(external or []),
                     bool(force_refresh), int(max_workers), store)
    finally:
        if own_store:
            store.close()


def _scan(domain, streams, days, major_days, max_per_source, request, keywords,
          external, force_refresh, max_workers, store) -> dict:
    started = time.perf_counter()
    now = datetime.now().astimezone()
    since = now - timedelta(days=days)
    keys = resolve_keys(domain, streams)
    run_id = store.start_run(domain, request)
    F.set_cache_ttl(0 if force_refresh else 300.0)

    limiter = HostLimiter()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(keys)))) as pool:
        futures = {pool.submit(_fetch_one, k, since, days, major_days, max_per_source,
                               limiter, force_refresh): k for k in keys}
        for fut in as_completed(futures):
            results.append(fut.result())

    report = []
    entries: list[dict] = []
    stale_feeds, sub_feed_failures, truncated, degraded = [], {}, {}, []
    for r in results:
        row = {"source": r["key"], "label": SOURCES[r["key"]]["label"],
               "status": r["status"], "count": len(r["entries"]),
               "seconds": r["seconds"]}
        if r["detail"]:
            row["detail"] = r["detail"]
        if r["archive_truncated"]:
            row["archive_truncated"] = r["archive_truncated"]
            truncated[r["key"]] = r["archive_truncated"]
        if r["sub_failed"]:
            sub_feed_failures[r["key"]] = r["sub_failed"]
        if r["stale"]:
            stale_feeds.append(r["stale"])
        report.append(row)
        entries.extend(r["entries"])
    report.sort(key=lambda x: keys.index(x["source"]))

    # ---- supplemental-search items are scored on the platform table --------
    external_added, dropped_external = 0, 0
    for it in external:
        if not isinstance(it, dict) or not (it.get("title") or "").strip():
            continue
        pub = it.get("date") or it.get("published")
        raw = F._strip_html(it.get("summary") or it.get("claim") or "")
        entry = {
            "title": F._strip_html(it.get("title") or ""),
            "url": it.get("url"),
            "summary": F._clip(raw),
            "summary_truncated": F._is_clipped(raw),
            "authors": it.get("authors") or [],
            "source_label": it.get("source") or it.get("source_label") or "补充检索",
            "source_key": it.get("source_key") or "",
            "published_raw": pub,
            "published": (F._parse_date(pub).isoformat() if F._parse_date(pub) else None),
            "external": True,
            "score_source": "platform_table",
            "in_window": None,
        }
        if it.get("signal_words"):
            entry["signal_words"] = it["signal_words"]
        if it.get("elements"):
            entry["elements"] = it["elements"]
        seen_in = [x for x in (it.get("also_seen_in") or []) if x]
        entry["also_seen_in"] = seen_in
        by_identity = len({identity_of(x) for x in seen_in if identity_of(x)}
                          | {identity_of(entry.get("source_label") or "")})
        if isinstance(it.get("pickups"), int) and it["pickups"] > 0:
            entry["_external_pickups"] = max(it["pickups"], by_identity)
        elif seen_in:
            entry["_external_pickups"] = by_identity
        dt = F._parse_date(entry["published"])
        if dt is not None and dt.date() < since.date():
            dropped_external += 1
            continue
        entries.append(entry)
        external_added += 1
    if external_added or dropped_external:
        report.append({"source": "external", "label": "supplemental search",
                       "status": "ok", "count": external_added,
                       "dropped_out_of_window": dropped_external})

    # ---- errata: Crossref files corrections as ordinary journal-articles ---
    errata_dropped = 0
    if ERRATA_DROP:
        kept_errata = []
        for entry in entries:
            if ERRATA_RE.search(entry.get("title") or ""):
                errata_dropped += 1
                continue
            kept_errata.append(entry)
        entries = kept_errata

    # ---- time window ------------------------------------------------------
    kept, undated, dropped_window, trend_exempt = [], 0, 0, 0
    for entry in entries:
        if SOURCES.get(entry.get("source_key", ""), {}).get("class") == "trend":
            entry.update(in_window=True, trend_no_date=True, is_trend=True)
            trend_exempt += 1
            kept.append(entry)
            continue
        dt = F._parse_date(entry.get("published"))
        if dt is None:
            entry["in_window"] = None
            undated += 1
            kept.append(entry)
        elif dt.date() >= since.date():
            entry["in_window"] = True
            kept.append(entry)
        elif (major_days > 0 and F._is_major_source(entry)
              and dt.date() >= (since - timedelta(days=major_days - days)).date()):
            entry["in_window"] = False
            entry["major_window"] = True
            entry["major_window_note"] = (
                f"outside the base {days}-day window; admitted as a major event "
                f"(window widened to {major_days} days). Cite the date, do not "
                "present it as recent.")
            kept.append(entry)
        else:
            entry["_dropped_window"] = True
            dropped_window += 1

    # ---- de-duplicate by EVENT, not by exact title ------------------------
    seen: list[dict] = []
    merged = 0
    for entry in kept:
        toks = title_tokens(entry.get("title") or "")
        if not toks:
            continue
        entry["event_id"] = event_id(entry.get("title") or "")
        hit = None
        for cand in seen:
            if is_same_event(toks, cand["_tokens"]):
                hit = cand
                break
        if hit is not None:
            hit.setdefault("also_seen_in", []).append(
                entry.get("source_key") or entry.get("source_label"))
            hit.setdefault("also_titles", []).append(entry.get("title"))
            entry["_merged_away"] = True
            merged += 1
        else:
            entry.setdefault("also_seen_in", [])
            entry.setdefault("also_titles", [])
            entry["_tokens"] = toks
            seen.append(entry)
    deduped = seen
    for entry in deduped:
        entry.pop("_tokens", None)

    # ---- what has this engine seen before? --------------------------------
    history = store.touch_items(deduped)
    for entry in deduped:
        h = history.get(entry.get("event_id") or "", {})
        first = h.get("first_seen")
        entry["first_seen"] = first
        entry["times_seen"] = h.get("times_seen", 0)
        entry["is_new"] = bool(
            h.get("times_seen", 0) == 0
            or (first and first[:10] >= since.date().isoformat()))

    # ---- credibility weights ---------------------------------------------
    run_domains = {domain}
    social_branch = domain == "social"
    for entry in deduped:
        key = entry.get("source_key", "")
        cls = SOURCES.get(key, {}).get("class", "")
        pickups = estimate_pickups(entry)
        if isinstance(entry.get("_external_pickups"), int) and entry["_external_pickups"] > 0:
            pickups = entry["_external_pickups"]
        entry.pop("_external_pickups", None)
        entry["evidence_type"] = evidence_type_for(key, cls, entry)
        entry["source_identity"] = identity_of(key)

        others = {identity_of(k) for k in (entry.get("also_seen_in") or []) if k}
        others.discard(entry["source_identity"])
        corroborated = bool(others) and cls != "trend"

        if entry.get("external"):
            entry["reconciliation"] = "platform-table(external)"
            entry["truth_score"] = external_truth_weight(entry)
            entry["comm_score"] = comm_weight_social(entry, pickups,
                                                     bool(entry.get("is_trend")))
            entry["score_source"] = "platform_table"
        elif bool(domain_of_source(key) & run_domains) and social_branch:
            entry["reconciliation"] = "social-evidence-form"
            entry["truth_score"] = truth_weight_social(entry, corroborated=corroborated)
            entry["comm_score"] = comm_weight_social(entry, pickups,
                                                     bool(entry.get("is_trend")))
        else:
            entry["reconciliation"] = "source-tier"
            entry["truth_score"] = truth_weight(key, cls, corroborated=corroborated,
                                                notices=entry.get("update_notices"),
                                                entry=entry)
            entry["comm_score"] = comm_weight(pickups, bool(entry.get("is_trend")))
        apply_trend_cap(entry)

        promo = promo_position(entry)
        entry["promotional_signal"] = promo == "title"
        if promo == "title":
            entry["truth_score"] = round(max(entry["truth_score"] - PROMO_PENALTY, 0.0), 3)
            entry["promo_note"] = f"promotional headline: truth -{PROMO_PENALTY} (signal, overridable)"
        elif promo == "body":
            entry["promo_body_hit"] = True
            entry["truth_score"] = round(max(entry["truth_score"] - PROMO_BODY_PENALTY, 0.0), 3)
            entry["promo_note"] = f"promotional wording in summary only: truth -{PROMO_BODY_PENALTY}"

        if body_missing(entry) and not entry.get("trend_capped"):
            entry["body_missing"] = True
            entry["truth_score"] = min(entry["truth_score"], 0.50)
            entry["body_note"] = "no verifiable body -> truth capped at 0.50 (below the gate)"

        if not entry.get("signal_words"):
            sw = signal_words(entry)
            if sw:
                entry["signal_words"] = sw

        cs = is_customer_story(entry)
        entry["customer_story"] = cs
        if cs:
            entry["customer_story_note"] = (
                "vendor customer story: not independent verification")

        entry["pickups"] = pickups
        entry["trend_boards"] = trend_saturation(entry)
        entry["corroborated_by"] = sorted(others)

    deduped.sort(key=lambda e: (e["truth_score"], e["comm_score"],
                                str(e.get("published") or ""),
                                str(e.get("title") or "")), reverse=True)

    # ---- per-source cap ---------------------------------------------------
    capped, per_source, overflow = [], {}, 0
    for entry in deduped:
        key = entry.get("source_key") or entry.get("source_label") or "?"
        if per_source.get(key, 0) >= max_per_source:
            overflow += 1
            continue
        per_source[key] = per_source.get(key, 0) + 1
        entry["_capped_in"] = True
        capped.append(entry)

    # ---- gates ------------------------------------------------------------
    counters = {"passed_both": 0, "passed_single": 0, "passed_truth": 0,
                "passed_comm": 0, "failed": 0, "retracted": 0}
    for entry in capped:
        sev = notice_severity(entry.get("update_notices") or [])
        if sev:
            entry["record_alert"] = sev
            if sev == "retracted":
                entry["truth_score"] = 0.0
                entry["passes_truth"] = False
                entry["passes_comm"] = entry["comm_score"] >= CREDIBILITY_THRESHOLD
                entry["passes_gate"] = False
                entry["section"] = "存疑区"
                entry["gate_note"] = "RETRACTED - void, must not be cited as fact"
                counters["retracted"] += 1
                counters["failed"] += 1
                continue

        t_ok = entry["truth_score"] >= CREDIBILITY_THRESHOLD
        c_ok = False if entry.get("trend_capped") else \
            entry["comm_score"] >= CREDIBILITY_THRESHOLD
        entry["passes_truth"] = t_ok
        entry["passes_comm"] = c_ok
        entry["corroborated"] = bool(c_ok)
        entry["single_source_hard_evidence"] = bool(
            t_ok and not c_ok and not entry.get("trend_capped")
            and not entry.get("customer_story")
            and entry.get("evidence_type") in HARD_EVIDENCE_TYPES)
        entry["passes_gate"] = bool(t_ok and (c_ok or entry["single_source_hard_evidence"]))

        if entry["passes_gate"] and entry["single_source_hard_evidence"]:
            entry["section"] = "核心区"
            entry["gate_note"] = (f"single-source hard evidence ({entry.get('evidence_type')}): "
                                  f"truth ok, comm={entry['comm_score']} - cite as single-source")
            counters["passed_single"] += 1
        elif entry["passes_gate"]:
            entry["section"] = "核心区"
            entry["gate_note"] = "both gates passed"
            counters["passed_both"] += 1
        elif t_ok and not c_ok:
            entry["section"] = "待观察区"
            entry["gate_note"] = "truth ok, comm below gate - verifiable but not yet spread"
            counters["passed_truth"] += 1
        elif c_ok and not t_ok:
            entry["section"] = "存疑区"
            entry["gate_note"] = "comm ok, truth below gate - widely spread, unverified"
            counters["passed_comm"] += 1
        elif entry.get("customer_story") and not c_ok:
            entry["section"] = "存疑区"
            entry["gate_note"] = "vendor customer story - not independent verification"
            counters["passed_comm"] += 1
        elif entry.get("trend_capped"):
            entry["section"] = "校准信号"
            entry["gate_note"] = "hot board: headline only, calibration signal, never content"
            counters["failed"] += 1
        else:
            entry["section"] = "丢弃"
            entry["gate_note"] = "both gates failed"
            counters["failed"] += 1

    capped.sort(key=lambda e: (e["passes_gate"], e["truth_score"], e["comm_score"]),
                reverse=True)

    # ---- per-source funnel (why did an item not land?) --------------------
    funnel: dict[str, dict] = {}
    for key in keys:
        funnel[key] = {"fetched": 0, "dropped_window": 0, "merged_into_other": 0,
                       "capped_out": 0, "emitted": 0}
    for e in entries:
        rec = funnel.setdefault(e.get("source_key") or "?", {
            "fetched": 0, "dropped_window": 0, "merged_into_other": 0,
            "capped_out": 0, "emitted": 0})
        rec["fetched"] += 1
        if e.get("_dropped_window"):
            rec["dropped_window"] += 1
        if e.get("_merged_away"):
            rec["merged_into_other"] += 1
    for e in capped:
        funnel.setdefault(e.get("source_key") or "?", {
            "fetched": 0, "dropped_window": 0, "merged_into_other": 0,
            "capped_out": 0, "emitted": 0})["emitted"] += 1
    for e in deduped:
        if not e.get("_capped_in"):
            funnel.setdefault(e.get("source_key") or "?", {
                "fetched": 0, "dropped_window": 0, "merged_into_other": 0,
                "capped_out": 0, "emitted": 0})["capped_out"] += 1
    for rec in funnel.values():
        rec["unaccounted"] = max(rec["fetched"] - rec["emitted"] - rec["merged_into_other"]
                                 - rec["capped_out"] - rec["dropped_window"], 0)
    for row in report:
        rec = funnel.get(row["source"])
        if rec is None:
            continue
        if row.get("seconds") is not None:
            rec["seconds"] = row["seconds"]
        if row["status"] == "ok" and not (row.get("count") or 0):
            rec["empty_feed"] = True

    # ---- degraded streams: silence must not look like full coverage -------
    for cls_name in ("authority", "social", "trend"):
        want = [k for k in keys if SOURCES[k].get("class") == cls_name]
        if not want:
            continue
        rows = [r for r in report if r["source"] in want]
        ok_rows = [r for r in rows if r["status"] == "ok"]
        got_n = sum(r.get("count", 0) for r in ok_rows)
        if not ok_rows:
            degraded.append({"stream": cls_name, "reason": "every source failed",
                             "attempted": len(want), "succeeded": 0, "entries": 0})
        elif got_n == 0:
            degraded.append({"stream": cls_name, "reason": "every source returned 0",
                             "attempted": len(want), "succeeded": len(ok_rows),
                             "entries": 0})

    # ---- relevance --------------------------------------------------------
    verdict = classify_request(request, [domain])
    classifier = verdict["verdict"] == "classifier"
    kws = intent_keywords(request, keywords)
    eff_kw = effective_keyword_count(kws)
    if not kws and not (request or "").strip():
        rel = {"mode": "none", "coverage": None,
               "note": "no request/keywords supplied; relevance not measured"}
    elif classifier and not kws:
        rel = domain_share(capped, run_domains)
        rel["mode"] = "classifier(domain-share)"
        rel["needs_second_pass"] = rel["coverage"] < 0.6
    elif classifier:
        kw_rel = keyword_coverage(kws, capped)
        dom_rel = domain_share(capped, run_domains)
        rel = dict(dom_rel)
        rel["mode"] = "classifier(domain-share; keyword advisory)"
        rel["keyword_advisory"] = {
            "coverage": kw_rel["coverage"], "keywords": kw_rel["keywords"],
            "matched": kw_rel["matched"], "missing": kw_rel["missing"],
            "note": "advisory only: category requests tokenise to whole clauses",
        }
        rel["needs_second_pass"] = dom_rel["coverage"] < 0.6
    else:
        rel = keyword_coverage(kws, capped)
        rel["mode"] = "keyword"
        if eff_kw < 2:
            rel["needs_second_pass"] = False
            rel["weak_validation"] = True
            rel["weak_reason"] = (f"only {eff_kw} effective keyword(s) (<2); "
                                  "coverage is recorded, not gated")
        else:
            rel["needs_second_pass"] = rel["coverage"] < 0.6
    rel["threshold"] = 0.6
    rel["request_type"] = verdict["verdict"]
    rel["effective_keywords"] = eff_kw
    rel.setdefault("weak_validation", eff_kw < 2)
    rel["classification"] = verdict
    if verdict["confidence"] <= 0.70:
        rel["classification_warning"] = (
            f"classification confidence {verdict['confidence']}: confirm the category "
            "with the user before trusting the stream selection")

    # ---- assemble ---------------------------------------------------------
    buckets = {"core": [], "watch": [], "suspect": [], "calibration": [],
               "discarded": []}
    for e in capped:
        buckets[BUCKET_OF_SECTION.get(e.get("section") or "", "discarded")].append(e)

    counts = {k: len(v) for k, v in buckets.items()}
    stats = {
        "requested_sources": len(keys),
        "succeeded_sources": sum(1 for r in report if r["status"] == "ok"),
        "failed_sources": sum(1 for r in report if r["status"] not in ("ok", "stale-feed")),
        "fetched": len(entries),
        "deduped": len(deduped),
        "emitted": len(capped),
        "overflow_capped": overflow,
        "merged": merged,
        "dropped_window": dropped_window,
        "undated": undated,
        "trend_exempt": trend_exempt,
        "errata_dropped": errata_dropped,
        "external_added": external_added,
        "buckets": counts,
        "gates": counters,
        "wall_seconds": round(time.perf_counter() - started, 2),
    }

    payload = {
        "run_id": run_id,
        "generated_at": now.isoformat(timespec="seconds"),
        "domain": domain,
        "streams": streams or ["social", "authority", "trend"],
        "window": {"days": days, "major_days": major_days,
                   "since": since.date().isoformat(),
                   "until": now.date().isoformat()},
        "request": request,
        "keywords": kws,
        "stats": stats,
        "coverage": {
            "sources_report": report,
            "source_funnel": funnel,
            "degraded_streams": degraded,
            "stale_feeds": stale_feeds,
            "sub_feed_failures": sub_feed_failures,
            "archive_truncated": truncated,
        },
        "relevance": rel,
        "entries": [_compact_entry(e) for e in capped],
    }
    path = store.finish_run(run_id, payload, stats)
    payload["file"] = str(path)
    return payload

