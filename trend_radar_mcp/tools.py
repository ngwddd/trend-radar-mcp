"""MCP tool surface.

Design rule: a tool returns a SMALL, decision-shaped answer. The raw material
lives in the run file and is fetched on demand through `radar_entries`. v1's
failure mode was the opposite: a 320 KB JSON that the model had to read whole,
then remember across eight stages.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import fetch as F
from .relevance import (classify_request, effective_keyword_count,
                        intent_keywords, keyword_coverage)
from .registry import BLOCKED, DOMAINS, SOURCES
from .scan import BUCKET_OF_SECTION, scan
from .scoring import (CREDIBILITY_THRESHOLD, comm_weight_social,
                      external_truth_weight, is_customer_story, platform_of,
                      signal_words, truth_weight_social)
from .store import Store


def _trim(entry: dict, chars: int = 320) -> dict:
    out = dict(entry)
    s = out.get("summary") or ""
    if len(s) > chars:
        out["summary"] = s[:chars] + "…"
    return out


TOOLS = [
    {
        "name": "radar_scan",
        "description": (
            "扫描一个领域（social/science/ai）的结构化情报源，并发采集 + 确定性评分 + 分桶。"
            "返回 run_id 与 核心区/待观察区/存疑区/校准信号 的头部条目；全量用 radar_entries 取。"
            "分数（truth_score / comm_score）由引擎算，0.6 是闸门，不要自己估；"
            "is_new/first_seen 来自本地状态库（跨次记忆），比模型猜测可靠。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": ["social", "science", "ai"],
                           "description": "三大类之一。判不准就先问用户，不要猜。"},
                "streams": {"type": "array", "items": {"type": "string",
                                                       "enum": ["social", "authority", "trend"]},
                            "description": "默认全部三路；只查一路时显式给。"},
                "days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 3},
                "major_days": {"type": "integer", "minimum": 0, "maximum": 14, "default": 0,
                               "description": "重大事件宽窗（只对期刊/官方源生效；进来的条目 in_window=false，引用时必须写明）。"},
                "max_per_source": {"type": "integer", "minimum": 5, "maximum": 100, "default": 30},
                "request": {"type": "string", "description": "用户原话，用于关联度判定。"},
                "keywords": {"type": "array", "items": {"type": "string"},
                             "description": "意图关键词 3-8 个；不给则从 request 抽。"},
                "external": {"type": "array", "items": {"type": "object"},
                             "description": "web_search 补来的条目（title/url/source/date/summary/also_seen_in），按平台表打分后并入。"},
                "force_refresh": {"type": "boolean", "default": False,
                                  "description": "默认 5 分钟磁盘缓存；true 强制重抓。"},
                "top": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6,
                        "description": "每个桶返回多少条头部条目。"},
            },
            "required": ["domain"],
        },
    },
    {
        "name": "radar_entries",
        "description": (
            "从一次 radar_scan 的 run 里取条目。可按 bucket / stream 过滤、分页。"
            "这是读取全量的唯一入口（run 文件不整份进上下文）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "bucket": {"type": "string", "enum": ["core", "watch", "suspect",
                                                      "calibration", "discarded"]},
                "stream": {"type": "string", "enum": ["social", "authority", "trend"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "summary_chars": {"type": "integer", "minimum": 120, "maximum": 1200, "default": 320},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "radar_sources",
        "description": "源注册表与可达性。含 42 个源的 class/domain、已知死路（不要再试）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": ["social", "science", "ai"]},
                "verbose": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "radar_score",
        "description": (
            "给外部条目（web_search / web_fetch 找到的）按同一套权重打分并分桶，"
            "不需要重跑采集。用于把补充检索并进同一条判据。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "minItems": 1, "items": {"type": "object"}},
                "domain": {"type": "string", "enum": ["social", "science", "ai"],
                           "default": "social"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "radar_relevance",
        "description": (
            "阶段5 关联度：量「萃取结果」是否还在回答原问题（方向性覆盖，不是对称相似）。"
            "<0.6 触发二次搜寻。可直接给 entries，或给 run_id 复用已抓条目。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}},
                "entries": {"type": "array", "items": {"type": "object"}},
                "run_id": {"type": "string"},
                "bucket": {"type": "string", "enum": ["core", "watch", "suspect",
                                                      "calibration", "discarded"]},
            },
        },
    },
    {
        "name": "radar_history",
        "description": (
            "查本地状态库：这个事件第一次是什么时候见到的、被见过几次。"
            "用来回答「这是真新，还是旧闻重发」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "query": {"type": "string", "description": "标题片段"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def h_scan(args: dict) -> dict:
    domain = args["domain"]
    top = int(args.get("top", 6))
    payload = scan(
        domain=domain,
        streams=args.get("streams"),
        days=int(args.get("days", 3)),
        major_days=int(args.get("major_days", 0)),
        max_per_source=int(args.get("max_per_source", 30)),
        request=args.get("request") or "",
        keywords=args.get("keywords") or [],
        external=args.get("external") or [],
        force_refresh=bool(args.get("force_refresh")),
    )
    buckets: dict[str, list] = {k: [] for k in
                                ("core", "watch", "suspect", "calibration", "discarded")}
    for e in payload["entries"]:
        b = e.get("bucket") or "discarded"
        if len(buckets[b]) < top:
            buckets[b].append(_trim(e))
    failed = [r for r in payload["coverage"]["sources_report"]
              if r["status"] not in ("ok", "stale-feed")]
    return {
        "run_id": payload["run_id"],
        "file": payload.get("file"),
        "domain": payload["domain"],
        "window": payload["window"],
        "stats": payload["stats"],
        "relevance": payload["relevance"],
        "coverage": {
            "degraded_streams": payload["coverage"]["degraded_streams"],
            "stale_feeds": payload["coverage"]["stale_feeds"],
            "failed_sources": failed[:12],
            "sub_feed_failures": payload["coverage"]["sub_feed_failures"],
        },
        "buckets": buckets,
        "next": ("radar_entries(run_id, bucket=...) 取全量；"
                 "写简报前先看 relevance.needs_second_pass 与 coverage。"),
    }


def h_entries(args: dict) -> dict:
    store = Store()
    try:
        run = store.get_run(args["run_id"])
    finally:
        store.close()
    if not run or "payload" not in run:
        raise ValueError(f"run not found: {args['run_id']}")
    entries = run["payload"].get("entries") or []
    bucket = args.get("bucket")
    stream = args.get("stream")
    if bucket:
        entries = [e for e in entries if e.get("bucket") == bucket]
    if stream:
        entries = [e for e in entries
                   if SOURCES.get(e.get("source_key") or "", {}).get("class") == stream
                   or (stream == "social" and e.get("source_key") == "external")]
    total = len(entries)
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", 20))
    page = entries[offset:offset + limit]
    chars = int(args.get("summary_chars", 320))
    return {
        "run_id": args["run_id"],
        "total": total,
        "offset": offset,
        "returned": len(page),
        "entries": [_trim(e, chars) for e in page],
    }


def h_sources(args: dict) -> dict:
    domain = args.get("domain")
    rows = []
    for key, spec in SOURCES.items():
        if domain and domain not in spec.get("domains", []):
            continue
        row = {"key": key, "label": spec["label"], "class": spec.get("class"),
               "group": spec.get("group"), "domains": spec.get("domains", []),
               "status": spec.get("status")}
        if args.get("verbose"):
            row["note"] = spec.get("note")
            row["kind"] = spec.get("kind")
        rows.append(row)
    return {
        "domains": DOMAINS,
        "counts": {k: sum(1 for r in rows if r["class"] == k)
                   for k in ("authority", "social", "trend")},
        "sources": rows,
        "blocked": [{"url": u, "reason": why} for u, why in BLOCKED] if args.get("verbose") else len(BLOCKED),
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
        entry["reconciliation"] = "social-evidence-form"
    else:
        entry["truth_score"] = external_truth_weight(entry)
        entry["reconciliation"] = "platform-table(external)"
    pickups = len({x for x in entry["also_seen_in"]} | {entry["source_label"]})
    if isinstance(it.get("pickups"), int) and it["pickups"] > 0:
        pickups = max(pickups, it["pickups"])
    entry["comm_score"] = comm_weight_social(entry, pickups, False)
    entry["pickups"] = pickups
    entry["customer_story"] = is_customer_story(entry)
    t_ok = entry["truth_score"] >= CREDIBILITY_THRESHOLD
    c_ok = entry["comm_score"] >= CREDIBILITY_THRESHOLD
    entry["passes_truth"] = t_ok
    entry["passes_comm"] = c_ok
    entry["section"] = ("核心区" if t_ok and c_ok else
                        "待观察区" if t_ok else
                        "存疑区" if c_ok else "丢弃")
    return entry


def h_score(args: dict) -> dict:
    domain = args.get("domain") or "social"
    scored = [_score_item(it, domain) for it in args["items"]
              if isinstance(it, dict) and (it.get("title") or "").strip()]
    order = {"核心区": 0, "待观察区": 1, "存疑区": 2, "丢弃": 3}
    scored.sort(key=lambda e: (order[e["section"]], -e["truth_score"], -e["comm_score"]))
    return {"domain": domain, "threshold": CREDIBILITY_THRESHOLD,
            "scored": [_trim(e) for e in scored]}


def h_relevance(args: dict) -> dict:
    entries = args.get("entries")
    if not entries and args.get("run_id"):
        store = Store()
        try:
            run = store.get_run(args["run_id"])
        finally:
            store.close()
        if not run or "payload" not in run:
            raise ValueError(f"run not found: {args['run_id']}")
        entries = run["payload"].get("entries") or []
        bucket = args.get("bucket")
        if bucket:
            entries = [e for e in entries if e.get("bucket") == bucket]
    if not entries:
        raise ValueError("give entries or run_id")
    kws = intent_keywords(args.get("request") or "", args.get("keywords"))
    eff = effective_keyword_count(kws)
    verdict = classify_request(args.get("request") or "", None)
    rel = keyword_coverage(kws, entries) if not (verdict["verdict"] == "classifier" and not kws) \
        else {"coverage": None, "note": "classifier request; pass a run_id for domain share"}
    rel.update({
        "request_type": verdict["verdict"],
        "classification": verdict,
        "effective_keywords": eff,
        "threshold": 0.6,
        "needs_second_pass": bool(
            (rel.get("coverage") is not None and rel["coverage"] < 0.6 and eff >= 2)),
        "weak_validation": eff < 2,
    })
    if eff < 2:
        rel["weak_reason"] = f"only {eff} effective keyword(s); coverage is recorded, not gated"
    return rel


def h_history(args: dict) -> dict:
    store = Store()
    try:
        rows = store.history(event_id=args.get("event_id"), query=args.get("query"),
                             limit=int(args.get("limit", 20)))
        stats = store.stats()
    finally:
        store.close()
    return {"db": stats, "items": rows}


HANDLERS = {
    "radar_scan": h_scan,
    "radar_entries": h_entries,
    "radar_sources": h_sources,
    "radar_score": h_score,
    "radar_relevance": h_relevance,
    "radar_history": h_history,
}
