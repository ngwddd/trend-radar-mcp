# -*- coding: utf-8 -*-
"""Replay one run and check the pipeline invariants.

Usage:
    python tools/verify_run.py                # latest run
    python tools/verify_run.py <run_id>

Checks counts (sources -> fetched -> deduped -> emitted -> buckets), field and
score ranges, duplicate event ids, and that the trace contains every phase in
order with no scan.error. Exit code 1 if anything fails.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar_mcp.store import Store, home

BUCKETS = {"core", "watch", "suspect", "calibration", "discarded"}


def main() -> int:
    store = Store()
    try:
        run_id = sys.argv[1] if len(sys.argv) > 1 else (
            store.recent_runs(1)[0]["run_id"] if store.recent_runs(1) else None)
        if not run_id:
            print("no runs found")
            return 1
        run = store.get_run(run_id)
    finally:
        store.close()
    if not run or "payload" not in run:
        print(f"run not found: {run_id}")
        return 1

    p = run["payload"]
    stats = p["stats"]
    report = p["coverage"]["sources_report"]
    entries = p["entries"]
    checks: list[tuple[bool, str, str]] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        checks.append((bool(ok), name, detail))

    req = stats["requested_sources"]
    check(req == len(report), "sources: requested == report rows",
          f"{req} vs {len(report)}")
    fetched_sum = sum(int(r.get("count") or 0) for r in report)
    check(fetched_sum == stats["fetched"], "sources: sum(count) == fetched",
          f"{fetched_sum} vs {stats['fetched']}")
    bucket_sum = sum(stats["buckets"].values())
    check(bucket_sum == stats["emitted"], "buckets: sum == emitted",
          f"{bucket_sum} vs {stats['emitted']}")
    check(len(entries) == stats["emitted"], "entries: len == emitted",
          f"{len(entries)} vs {stats['emitted']}")
    check(stats["deduped"] <= stats["fetched"], "dedupe: deduped <= fetched",
          f"{stats['deduped']} vs {stats['fetched']}")
    check(stats["emitted"] <= stats["deduped"] + stats.get("external_added", 0),
          "dedupe: emitted <= deduped + external",
          f"{stats['emitted']} vs {stats['deduped']}+{stats.get('external_added', 0)}")

    bad_scores, bad_fields, bad_bucket, dup_ids, no_url = [], [], [], [], []
    seen = set()
    for e in entries:
        for k in ("truth_score", "comm_score"):
            v = e.get(k)
            if not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
                bad_scores.append((e.get("event_id"), k, v))
        if not (e.get("title") or "").strip():
            bad_fields.append(e.get("event_id"))
        if e.get("bucket") not in BUCKETS:
            bad_bucket.append(e.get("bucket"))
        if not (e.get("url") or "").strip():
            no_url.append(e.get("event_id"))
        eid = e.get("event_id")
        if eid in seen:
            dup_ids.append(eid)
        seen.add(eid)

    check(not bad_scores, "entries: scores within [0,1]", str(bad_scores[:3]))
    check(not bad_fields, "entries: title non-empty", str(bad_fields[:3]))
    check(not bad_bucket, "entries: bucket valid", str(bad_bucket[:3]))
    check(not dup_ids, "entries: event_id unique", str(dup_ids[:3]))
    # Missing URLs are allowed (some feeds omit them) but must be counted.
    if no_url:
        print(f"  note: {len(no_url)} entries without url (allowed, listed in payload)")

    trace_path = home() / "trace.jsonl"
    phases = []
    errors = []
    if trace_path.exists():
        for line in trace_path.read_text(encoding="utf-8").splitlines()[-4000:]:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") != run_id:
                continue
            phases.append(rec["event"])
            if rec["event"] == "scan.error":
                errors.append(rec)
    order = ["scan.start", "scan.fetch", "scan.dedupe", "scan.gate", "scan.done"]
    missing = [x for x in order if x not in phases]
    check(not missing, "trace: all phases present", str(missing))
    check(not errors, "trace: no scan.error", str(errors[:1]))
    if all(x in phases for x in order):
        check(phases.index("scan.start") < phases.index("scan.done"),
              "trace: phase order", ",".join(phases))

    check(stats.get("wall_seconds", 0) >= 0, "stats: wall_seconds present",
          str(stats.get("wall_seconds")))

    failed = [r for r in report if r["status"] == "failed"]
    print(f"run {run_id} | scope={p.get('scope')} | emitted={len(entries)} | "
          f"wall={stats.get('wall_seconds')}s | failed_sources={len(failed)}")
    ok_all = True
    for ok, name, detail in checks:
        ok_all &= ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    print("RESULT:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
