"""MCP tool surface v0.3: three tools, one round trip when possible.

v0.2 was already cheap per byte, but it still cost the model 4 turns:
classify -> scan -> entries -> write. v0.3 removes two of them:

  * scope is classified INSIDE `radar_scan` (explicit domain still wins);
  * the top core/watch items come back WITH their summaries (`briefs`), so a
    normal briefing needs one call, not two.

The remaining tools are `radar_pack` (more detail for chosen ids) and
`radar_check` (relevance + scoring of supplemental search results).
"""

from __future__ import annotations

import json

from . import fetch as F
from .relevance import (classify_request, effective_keyword_count,
                        intent_keywords, keyword_coverage)
from .registry import DOMAINS, SOURCES
from .scan import scan
from .scoring import (CREDIBILITY_THRESHOLD, comm_weight, external_truth_weight,
                      is_customer_story, platform_of, signal_words,
                      truth_weight_social)
from .store import Store

BUCKETS = ("core", "watch", "suspect", "calibration", "discarded")


def _digest_item(e: dict, summary_chars: int = 0) -> dict:
    flag = ""
    if e.get("record_alert") == "retracted":
        flag = "retracted"
    elif e.get("single_source_hard_evidence"):
        flag = "single"
    elif e.get("customer_story"):
        flag = "customer"
    item = {
        "id": e.get("event_id"),
        "bucket": e.get("bucket"),
        "truth": e.get("truth_score"),
        "comm": e.get("comm_score"),
        "title": (e.get("title") or "")[:80],
        "source": (e.get("source") or "")[:24],
        "date": (e.get("published") or "")[:10],
        "new": bool(e.get("is_new")),
        "flag": flag,
    }
    if e.get("borderline"):
        item["borderline"] = True
    if summary_chars:
        s = e.get("summary") or ""
        item["summary"] = s[:summary_chars] + ("…" if len(s) > summary_chars else "")
    return item


TOOLS = [
    {
        "name": "radar_scan",
        "description": (
            "采集 + 评分 + 分桶，一次调用给出可写简报的材料。"
            "\n\nscope 自动判定：不给 domain 时按 query 判三类；判不进（或跨类）→ broad 全量 42 源。"
            "显式 domain 永远优先。返回 scope_source 说明是 auto / explicit / auto-broad。"
            "\n\n返回 digest（每桶 top N：id/标题/truth·comm/来源/日期/new），"
            "并对 core、watch 的前 `briefs` 条**内联摘要**（默认 3 条 × 240 字）。"
            "多数情况下一次调用就够写；要更多正文再用 radar_pack。"
            "\n\n硬约束：truth/comm 由引擎算，0.6 是闸门，禁止自己估；"
            "borderline=true 表示分数在 0.55–0.65，必须复核后再引用；"
            "calibration 桶只校准传播度，永不作内容源。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "用户原话。用于自动判 scope 与关联度，不要改写。"},
                "domain": {"type": "string",
                           "enum": ["auto", "social", "science", "ai", "broad"],
                           "default": "auto",
                           "description": "auto=按 query 判；判不进自动 broad。显式给则覆盖。"},
                "streams": {"type": "array", "items": {"type": "string",
                                                       "enum": ["social", "authority", "trend"]},
                            "description": "默认三路。只查一路时给，省时间。"},
                "days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 3},
                "major_days": {"type": "integer", "minimum": 0, "maximum": 14, "default": 0,
                               "description": "重大事件宽窗。进来的条目 in_window=false，引用必须写明日期不在基础窗口。"},
                "max_per_source": {"type": "integer", "minimum": 5, "maximum": 100, "default": 20},
                "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "external": {"type": "array", "items": {"type": "object"}, "maxItems": 10,
                             "description": "web_search 补搜条目（title/url/source/date/summary）。"},
                "briefs": {"type": "integer", "minimum": 0, "maximum": 8, "default": 3,
                           "description": "内联多少条摘要（core 优先，再 watch）。0=只要 digest。"},
                "brief_chars": {"type": "integer", "minimum": 120, "maximum": 400, "default": 240},
                "deep": {"type": "boolean", "default": False,
                         "description": "true 才抓 arXiv API（4 分类 × 3s 礼貌间隔，最慢的源）。默认跳过，用它的 RSS 代替。"},
                "force_refresh": {"type": "boolean", "default": False},
                "top": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4},
            },
            "required": ["query"],
        },
    },
    {
        "name": "radar_pack",
        "description": (
            "按 id / bucket 取正文与细节。只在 digest 不够写的时候用。"
            "\n硬约束：ids ≤ 12；limit ≤ 20；默认只给 scores+flags，"
            "要正文必须显式 fields=['summary']；摘要默认 240 字。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12,
                        "description": "digest 里的 id（支持前缀）。给了 ids 就忽略 bucket。"},
                "bucket": {"type": "string", "enum": list(BUCKETS)},
                "fields": {"type": "array", "items": {"type": "string",
                                                      "enum": ["summary", "scores", "flags"]},
                           "default": ["scores", "flags"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "summary_chars": {"type": "integer", "minimum": 120, "maximum": 600, "default": 240},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "radar_check",
        "description": (
            "发布前自检：关联度（方向性覆盖）+ 补搜条目打分，一次调用。"
            "\n硬约束：量的是准备发布的条目（bucket=core 或显式 entries），不要传全池；"
            "关键词 ≤ 8；补搜条目 ≤ 10 且必须带 url；<0.6 必须重查后再写。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "bucket": {"type": "string", "enum": list(BUCKETS), "default": "core"},
                "entries": {"type": "array", "items": {"type": "object"}, "maxItems": 30},
                "query": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "external": {"type": "array", "items": {"type": "object"}, "maxItems": 10},
                "domain": {"type": "string", "enum": ["social", "science", "ai", "broad"],
                           "default": "broad"},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

GENERIC_SOCIAL_TOKENS = {"热点", "新闻", "事件", "舆情"}


def _auto_scope(query: str, domain: str | None) -> tuple[str, dict]:
    if domain and domain not in ("auto", ""):
        return ("broad" if domain == "broad" else domain), {"source": "explicit"}
    verdict = classify_request(query or "", None)
    matched = verdict.get("matched") or {}
    # "AI 圈有什么热点" hits ai + social only because 热点 is a request-style
    # word, not because the user asked for social news. Drop social when its
    # only hit is generic news vocabulary.
    if (len(matched) == 2 and "social" in matched
            and set(matched["social"]) <= GENERIC_SOCIAL_TOKENS):
        other = next(k for k in matched if k != "social")
        return other, {**verdict, "source": "auto",
                       "note": "social dropped: matched only generic news words"}
    if verdict["verdict"] == "classifier" and len(matched) == 1 and verdict["confidence"] >= 0.7:
        return next(iter(matched)), {**verdict, "source": "auto"}
    return "broad", {**verdict, "source": "auto-broad"}


DIGEST_DOMAINS = ("social", "ai", "science")


def _entry_domain(e: dict) -> str:
    key = e.get("source_key") or ""
    if key == "external" or not key:
        return "social"
    doms = SOURCES.get(key, {}).get("domains") or []
    for d in DIGEST_DOMAINS:
        if d in doms:
            return d
    return "social"


def _digest_pick(entries: list[dict], top: int, scope: str) -> list[dict]:
    """Pick the head of one bucket.

    broad: round-robin across social/AI/science so one high-truth general outlet
    cannot occupy the whole digest (measured: AI queries were headed by French
    protests). narrow: entries matching the scope lexicon first, then the rest.
    """
    if not entries:
        return []
    if scope == "broad":
        buckets: dict[str, list] = {d: [] for d in DIGEST_DOMAINS}
        for e in entries:
            buckets.setdefault(_entry_domain(e), []).append(e)
        out: list[dict] = []
        while len(out) < top and any(buckets.values()):
            for d in DIGEST_DOMAINS:
                if buckets.get(d):
                    out.append(buckets[d].pop(0))
                if len(out) >= top:
                    break
        return out
    preferred = [e for e in entries if (e.get("topic_hits") or 0) > 0]
    rest = [e for e in entries if (e.get("topic_hits") or 0) <= 0]
    return (preferred + rest)[:top]

def _briefs_for(payload: dict, count: int, chars: int) -> dict[str, list]:
    """Inline summaries for the first `count` core items, then watch."""
    out = {"core": [], "watch": []}
    left = count
    for bucket in ("core", "watch"):
        for e in payload["entries"]:
            if left <= 0:
                break
            if e.get("bucket") != bucket:
                continue
            out[bucket].append(_digest_item(e, summary_chars=chars))
            left -= 1
    return out


def h_scan(args: dict) -> dict:
    query = args.get("query") or ""
    scope, classification = _auto_scope(query, args.get("domain"))
    payload = scan(
        domain=None if scope == "broad" else scope,
        streams=args.get("streams"),
        days=int(args.get("days", 3)),
        major_days=int(args.get("major_days", 0)),
        max_per_source=int(args.get("max_per_source", 20)),
        request=query,
        keywords=args.get("keywords") or [],
        external=args.get("external") or [],
        force_refresh=bool(args.get("force_refresh")),
        deep=bool(args.get("deep")),
    )
    top = int(args.get("top", 4))
    digest: dict[str, list] = {b: [] for b in BUCKETS}
    for b in BUCKETS:
        bucket_entries = [e for e in payload["entries"] if (e.get("bucket") or "discarded") == b]
        digest[b] = [_digest_item(e) for e in _digest_pick(bucket_entries, top, scope)]
    briefs = _briefs_for(payload, int(args.get("briefs", 3)),
                         int(args.get("brief_chars", 240)))

    failed = [{"source": r["source"], "reason": (r.get("detail") or r["status"])[:60]}
              for r in payload["coverage"]["sources_report"]
              if r["status"] not in ("ok", "stale-feed", "skipped-empty")]
    skipped = [r["source"] for r in payload["coverage"]["sources_report"]
               if r["status"] == "skipped-empty"]
    result = {
        "run_id": payload["run_id"],
        "scope": payload.get("scope"),
        "scope_source": classification.get("source"),
        "classification": {"verdict": classification.get("verdict"),
                           "matched": list((classification.get("matched") or {}).keys()),
                           "confidence": classification.get("confidence")},
        "window": payload["window"],
        "stats": {
            "sources": payload["stats"]["requested_sources"],
            "ok": payload["stats"]["succeeded_sources"],
            "failed": payload["stats"]["failed_sources"],
            "skipped_empty": payload["stats"].get("skipped_empty_sources", 0),
            "skipped_slow": payload["stats"].get("skipped_slow", 0),
            "emitted": payload["stats"]["emitted"],
            "wall_s": payload["stats"]["wall_seconds"],
            "buckets": payload["stats"]["buckets"],
        },
        "digest": digest,
        "briefs": briefs,
        "relevance": payload["relevance"],
        "coverage": {
            "degraded_streams": payload["coverage"]["degraded_streams"],
            "stale_feeds": payload["coverage"]["stale_feeds"],
            "failed_sources": failed[:8],
            "skipped_empty_sources": skipped,
            "stream_fallback": payload.get("stream_fallback", False),
        },
        "notice": ("core 为空：今天没有同时通过真值与传播闸门的条目；"
                   "watch 是可信但尚未被独立转载的" if not digest["core"] else ""),
        "next": ("digest + briefs 已够写就直接写；不够再 radar_pack(ids≤12, fields=['summary'])；"
                 "写完用 radar_check 量关联度。"),
    }
    return _fit(result)


def _fit(result: dict, limit: int = 8000) -> dict:
    text = json.dumps(result, ensure_ascii=False)
    for bucket in ("discarded", "calibration", "suspect", "watch"):
        if len(text.encode()) <= limit:
            break
        if result["digest"].get(bucket):
            result["digest"][bucket].pop()
            text = json.dumps(result, ensure_ascii=False)
    while len(text.encode()) > limit and result["briefs"].get("watch"):
        result["briefs"]["watch"].pop()
        text = json.dumps(result, ensure_ascii=False)
    return result


def _project(e: dict, fields: set[str], chars: int) -> dict:
    out = {"id": e.get("event_id"), "bucket": e.get("bucket"),
           "title": e.get("title"), "url": e.get("url"),
           "source": e.get("source"), "date": e.get("published")}
    if "summary" in fields:
        s = e.get("summary") or ""
        out["summary"] = s[:chars] + ("…" if len(s) > chars else "")
    if "scores" in fields:
        out.update({"truth": e.get("truth_score"), "comm": e.get("comm_score"),
                    "section": e.get("section"), "gate": e.get("gate_note"),
                    "evidence_type": e.get("evidence_type"),
                    "pickups": e.get("pickups"),
                    "borderline": bool(e.get("borderline"))})
    if "flags" in fields:
        out.update({"new": bool(e.get("is_new")), "first_seen": e.get("first_seen"),
                    "times_seen": e.get("times_seen"),
                    "alert": e.get("record_alert"),
                    "single": bool(e.get("single_source_hard_evidence")),
                    "customer": bool(e.get("customer_story")),
                    "promo": bool(e.get("promotional_signal")),
                    "signal_words": e.get("signal_words") or [],
                    "in_window": e.get("in_window"),
                    "major_window": e.get("major_window")})
    return out


def h_pack(args: dict) -> dict:
    store = Store()
    try:
        run = store.get_run(args["run_id"])
    finally:
        store.close()
    if not run or "payload" not in run:
        raise ValueError(f"run not found: {args['run_id']}")
    entries = run["payload"].get("entries") or []
    ids = [x.strip() for x in (args.get("ids") or []) if x and x.strip()]
    if ids:
        entries = [e for e in entries
                   if any((e.get("event_id") or "").startswith(i) for i in ids)]
    elif args.get("bucket"):
        entries = [e for e in entries if e.get("bucket") == args["bucket"]]
    total = len(entries)
    offset = int(args.get("offset", 0))
    limit = min(int(args.get("limit", 8)), 20)
    fields = set(args.get("fields") or ["scores", "flags"])
    page = entries[offset:offset + limit]
    return {
        "run_id": args["run_id"],
        "total": total,
        "returned": len(page),
        "entries": [_project(e, fields, int(args.get("summary_chars", 240))) for e in page],
    }


def _score_item(it: dict, domain: str) -> dict:
    entry = {
        "title": F._strip_html(it.get("title") or ""),
        "summary": F._strip_html(it.get("summary") or it.get("claim") or ""),
        "url": it.get("url"),
        "source_label": it.get("source") or it.get("source_label") or "补充检索",
        "source_key": it.get("source_key") or "",
        "published": it.get("date") or it.get("published"),
        "also_seen_in": [x for x in (it.get("also_seen_in") or []) if x],
        "authors": it.get("authors") or [],
    }
    entry["signal_words"] = it.get("signal_words") or signal_words(entry)
    entry["platform_key"] = platform_of(entry["source_key"], entry["source_label"])
    if domain == "social":
        entry["truth_score"] = truth_weight_social(entry)
    else:
        entry["truth_score"] = external_truth_weight(entry)
    pickups = len({x for x in entry["also_seen_in"]} | {entry["source_label"]})
    entry["comm_score"] = comm_weight(pickups, False)
    entry["pickups"] = pickups
    entry["customer_story"] = is_customer_story(entry)
    t_ok = entry["truth_score"] >= CREDIBILITY_THRESHOLD
    c_ok = entry["comm_score"] >= CREDIBILITY_THRESHOLD
    entry["section"] = ("核心区" if t_ok and c_ok else
                        "待观察区" if t_ok else "存疑区" if c_ok else "丢弃")
    return entry


def h_check(args: dict) -> dict:
    entries = args.get("entries")
    bucket = args.get("bucket") or "core"
    available: dict = {}
    run_request = ""
    if entries is None and args.get("run_id"):
        store = Store()
        try:
            run = store.get_run(args["run_id"])
        finally:
            store.close()
        if not run or "payload" not in run:
            raise ValueError(f"run not found: {args['run_id']}")
        all_entries = run["payload"].get("entries") or []
        run_request = run["payload"].get("request") or ""
        for e in all_entries:
            available[e.get("bucket") or "discarded"] = available.get(e.get("bucket") or "discarded", 0) + 1
        entries = [e for e in all_entries if e.get("bucket") == bucket]
    if entries is None:
        raise ValueError("give entries or run_id")

    # An empty bucket is a finding, not an error: a run can legitimately have
    # zero core items (structural for single-source peer-reviewed days).
    if not entries:
        return {
            "bucket": bucket,
            "empty": True,
            "available_buckets": available,
            "relevance": {"coverage": None, "threshold": 0.6,
                          "needs_second_pass": False,
                          "note": f"no entries in bucket {bucket!r}; "
                                  "check watch/suspect or widen the window"},
        }

    kws = intent_keywords(args.get("query") or run_request, args.get("keywords"))
    eff = effective_keyword_count(kws)
    rel = keyword_coverage(kws, entries)
    rel.update({
        "threshold": 0.6,
        "effective_keywords": eff,
        "needs_second_pass": bool(rel.get("coverage") is not None
                                  and rel["coverage"] < 0.6 and eff >= 2),
        "weak_validation": eff < 2,
    })
    if eff < 2:
        rel["weak_reason"] = f"only {eff} effective keyword(s); coverage recorded, not gated"

    out = {"relevance": rel}
    external = args.get("external") or []
    if external:
        scored = [_score_item(it, args.get("domain") or "broad") for it in external
                  if isinstance(it, dict) and (it.get("title") or "").strip()
                  and (it.get("url") or "").strip()]
        out["scored"] = [{"title": e["title"][:70], "url": e["url"],
                          "truth": e["truth_score"], "comm": e["comm_score"],
                          "section": e["section"]} for e in scored]
    return out


HANDLERS = {
    "radar_scan": h_scan,
    "radar_pack": h_pack,
    "radar_check": h_check,
}
