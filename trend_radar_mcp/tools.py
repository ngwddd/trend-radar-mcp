"""MCP tool surface: digest first, details on demand.

v0.1 returned full compact entries from `radar_scan` (~10 KB) and made the
model read them before it knew which ones mattered. v0.2 returns a *digest*:
id, bucket, two scores, title, source, date, one flag. Summaries are fetched
only for the ids the model actually chooses.

Token rules enforced here, not left to the model:
  scan response  <= 6 KB   (digest + problems)
  entries call   <= 4 KB   (default fields, <= 12 items, 300-char summaries)
  score call     <= 20 items
"""

from __future__ import annotations

import json

from . import fetch as F
from .relevance import (classify_request, effective_keyword_count,
                        intent_keywords, keyword_coverage)
from .registry import BLOCKED, DOMAINS, SOURCES
from .scan import BUCKET_OF_SECTION, scan
from .scoring import (CREDIBILITY_THRESHOLD, comm_weight_social,
                      external_truth_weight, is_customer_story, platform_of,
                      signal_words, truth_weight_social)
from .store import Store

BUCKETS = ("core", "watch", "suspect", "calibration", "discarded")
FLAGS = {"single": "单源硬证据", "retracted": "已撤稿", "customer": "厂商客户案例"}


def _digest_item(e: dict) -> dict:
    flag = ""
    if e.get("record_alert") == "retracted":
        flag = "retracted"
    elif e.get("single_source_hard_evidence"):
        flag = "single"
    elif e.get("customer_story"):
        flag = "customer"
    return {
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


TOOLS = [
    {
        "name": "radar_scan",
        "description": (
            "扫描结构化情报源（42 个：期刊/官方/媒体/社区/热榜），并发采集 + 固定权重评分 + 分桶。"
            "返回 run_id 和 digest（每桶 top N 的 id/标题/两个分数），不返回摘要正文。"
            "\n\n何时用 broad：需求无法判进 social/science/ai 三类，或用户明确要「全面搜」。"
            "broad = 全部 42 源、三路全开，比单类慢约 1 倍、贵约 1 倍，不要默认用。"
            "\n\n硬约束：truth/comm 是引擎算的，0.6 为闸门，禁止自己估分或合成一个分；"
            "is_new 来自本地状态库（first_seen），比模型印象可靠；"
            "digest 里 calibration 桶只做传播度校准，永不作内容源。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": ["social", "science", "ai", "broad"],
                           "description": "省略 = broad。判不准时用 broad，不要猜一个类。"},
                "streams": {"type": "array", "items": {"type": "string",
                                                       "enum": ["social", "authority", "trend"]},
                            "description": "默认三路全开。只要一路时显式给，省一半时间。"},
                "days": {"type": "integer", "minimum": 1, "maximum": 30, "default": 3},
                "major_days": {"type": "integer", "minimum": 0, "maximum": 14, "default": 0,
                               "description": "重大事件宽窗（只对期刊/官方源生效）。进来的条目 in_window=false，引用必须写明日期不在基础窗口内。"},
                "max_per_source": {"type": "integer", "minimum": 5, "maximum": 100, "default": 20,
                                   "description": "单源上限。默认 20：实测 30 只多 9 秒、多 20% 噪声，对结论没有增益。"},
                "request": {"type": "string", "description": "用户原话，用于关联度判定。"},
                "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                             "description": "意图关键词 3–8 个；中文整句会被拆成从句，不要给整句。"},
                "external": {"type": "array", "items": {"type": "object"}, "maxItems": 20,
                             "description": "web_search 补来的条目（title/url/source/date/summary），按平台表打分后并入同一闸门。"},
                "force_refresh": {"type": "boolean", "default": False,
                                  "description": "默认源级缓存 5 分钟；true 强制重抓（慢 10 倍，非必要不要用）。"},
                "top": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3,
                        "description": "每桶返回多少条 digest。core 可给 6–8，其余 2–3。"},
            },
        },
    },
    {
        "name": "radar_entries",
        "description": (
            "按 id / bucket / stream 取条目正文，是读取摘要的唯一入口。"
            "先用 digest 选 id，再只对这些 id 取 summary —— 不要整桶拉。"
            "\n\n硬约束：一次最多 20 个 id / 50 条；默认字段只含 title/url/source/date + 分数，"
            "要 summary 必须显式写 fields=['summary']；摘要默认截到 300 字。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20,
                        "description": "digest 里的 id（支持前缀匹配）。优先级高于 bucket。"},
                "bucket": {"type": "string", "enum": list(BUCKETS)},
                "stream": {"type": "string", "enum": ["social", "authority", "trend"]},
                "fields": {"type": "array", "items": {"type": "string",
                                                      "enum": ["summary", "scores", "flags"]},
                           "description": "默认 ['scores','flags']；要正文加 'summary'；不要一次全开。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "summary_chars": {"type": "integer", "minimum": 120, "maximum": 800, "default": 300},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "radar_sources",
        "description": (
            "源注册表：42 源的 class/domain、46 条已知死路（不要再试）、"
            "以及本地状态库里的连续空源（这些会被自动跳过 12 小时）。"
        ),
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
            "给 web_search / web_fetch 找到的条目按同一套权重打分并分桶，不重跑采集。"
            "用在「主池没有、但检索到了」的补充材料上，最多 20 条。"
            "\n硬约束：必须带 url；没有 url 的条目不要传（无法核查等于噪声）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "minItems": 1, "maxItems": 20,
                          "items": {"type": "object"}},
                "domain": {"type": "string", "enum": ["social", "science", "ai"],
                           "default": "social"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "radar_relevance",
        "description": (
            "阶段 5 关联度：量「准备发布的条目」是否还在回答原问题（方向性覆盖，不是相似度）。"
            "<0.6 触发二次搜寻。优先传 run_id + bucket=core（量萃取结果），不要传全池 —— 全池会让闸门失效。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "entries": {"type": "array", "items": {"type": "object"}, "maxItems": 50},
                "run_id": {"type": "string"},
                "bucket": {"type": "string", "enum": list(BUCKETS), "default": "core"},
            },
        },
    },
    {
        "name": "radar_history",
        "description": (
            "本地状态库：某事件第一次见到的时间、被见过几次、最近一次是哪天。"
            "用来回答「这是真新，还是旧闻重发」；也是 broad 与单类扫描共用的去重底账。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "query": {"type": "string", "description": "标题片段"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def _buckets_from(payload: dict, top: int) -> dict:
    out: dict[str, list] = {b: [] for b in BUCKETS}
    for e in payload.get("entries") or []:
        b = e.get("bucket") or "discarded"
        if len(out[b]) < top:
            out[b].append(_digest_item(e))
    return out


def h_scan(args: dict) -> dict:
    domain = args.get("domain") or "broad"
    top = int(args.get("top", 3))
    payload = scan(
        domain=None if domain == "broad" else domain,
        streams=args.get("streams"),
        days=int(args.get("days", 3)),
        major_days=int(args.get("major_days", 0)),
        max_per_source=int(args.get("max_per_source", 20)),
        request=args.get("request") or "",
        keywords=args.get("keywords") or [],
        external=args.get("external") or [],
        force_refresh=bool(args.get("force_refresh")),
    )
    failed = [{"source": r["source"], "reason": (r.get("detail") or r["status"])[:70]}
              for r in payload["coverage"]["sources_report"]
              if r["status"] not in ("ok", "stale-feed")]
    result = {
        "run_id": payload["run_id"],
        "scope": payload.get("scope"),
        "window": payload["window"],
        "stats": {
            "sources": payload["stats"]["requested_sources"],
            "ok": payload["stats"]["succeeded_sources"],
            "failed": payload["stats"]["failed_sources"],
            "skipped_empty": payload["stats"].get("skipped_empty_sources", 0),
            "emitted": payload["stats"]["emitted"],
            "wall_s": payload["stats"]["wall_seconds"],
            "buckets": payload["stats"]["buckets"],
            "gates": payload["stats"]["gates"],
        },
        "digest": _buckets_from(payload, top),
        "relevance": payload["relevance"],
        "coverage": {
            "degraded_streams": payload["coverage"]["degraded_streams"],
            "stale_feeds": payload["coverage"]["stale_feeds"],
            "failed_sources": failed[:10],
            "stream_fallback": payload.get("stream_fallback", False),
        },
        "next": ("radar_entries(run_id, ids=[...], fields=['summary']) 只取你要写的条目；"
                 "core 可引用、watch 不是噪声、calibration 只作传播校准。"),
    }
    return _fit(result)


def _fit(result: dict, limit: int = 6500) -> dict:
    """Keep the scan response inside its budget by trimming the tail buckets."""
    text = json.dumps(result, ensure_ascii=False)
    for bucket in ("discarded", "calibration", "suspect", "watch"):
        if len(text.encode()) <= limit:
            break
        items = result["digest"].get(bucket) or []
        if items:
            items.pop()
            text = json.dumps(result, ensure_ascii=False)
    return result


def _project(e: dict, fields: set[str], chars: int) -> dict:
    out = {"id": e.get("event_id"), "bucket": e.get("bucket"),
           "title": e.get("title"), "url": e.get("url"),
           "source": e.get("source"), "date": e.get("published")}
    if "summary" in fields:
        s = e.get("summary") or ""
        out["summary"] = s[:chars] if len(s) > chars else s
    if "scores" in fields:
        out.update({"truth": e.get("truth_score"), "comm": e.get("comm_score"),
                    "section": e.get("section"), "gate": e.get("gate_note"),
                    "evidence_type": e.get("evidence_type"),
                    "pickups": e.get("pickups")})
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


def h_entries(args: dict) -> dict:
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
        entries = [e for e in entries if any((e.get("event_id") or "").startswith(i) for i in ids)]
    else:
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
    limit = min(int(args.get("limit", 12)), 50)
    fields = set(args.get("fields") or ["scores", "flags"])
    page = entries[offset:offset + limit]
    return {
        "run_id": args["run_id"],
        "total": total,
        "returned": len(page),
        "entries": [_project(e, fields, int(args.get("summary_chars", 300))) for e in page],
    }


def h_sources(args: dict) -> dict:
    domain = args.get("domain")
    rows = []
    for key, spec in SOURCES.items():
        if domain and domain not in spec.get("domains", []):
            continue
        row = {"key": key, "label": spec["label"], "class": spec.get("class"),
               "group": spec.get("group"), "domains": spec.get("domains", [])}
        if args.get("verbose"):
            row["note"] = spec.get("note")
            row["kind"] = spec.get("kind")
        rows.append(row)
    store = Store()
    try:
        health = store.health()
    finally:
        store.close()
    return {
        "domains": DOMAINS,
        "counts": {k: sum(1 for r in rows if r["class"] == k)
                   for k in ("authority", "social", "trend")},
        "sources": rows,
        "blocked": [{"url": u, "reason": why} for u, why in BLOCKED] if args.get("verbose") else len(BLOCKED),
        "empty_streak": [h for h in health if h.get("empty_streak", 0) >= 3],
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
              if isinstance(it, dict) and (it.get("title") or "").strip()
              and (it.get("url") or "").strip()]
    order = {"核心区": 0, "待观察区": 1, "存疑区": 2, "丢弃": 3}
    scored.sort(key=lambda e: (order[e["section"]], -e["truth_score"], -e["comm_score"]))
    return {"domain": domain, "threshold": CREDIBILITY_THRESHOLD,
            "scored": [{"title": e["title"][:80], "url": e["url"],
                        "truth": e["truth_score"], "comm": e["comm_score"],
                        "pickups": e["pickups"], "section": e["section"]}
                       for e in scored]}


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
        bucket = args.get("bucket") or "core"
        entries = [e for e in entries if e.get("bucket") == bucket]
    if not entries:
        raise ValueError("give entries or run_id")
    kws = intent_keywords(args.get("request") or "", args.get("keywords"))
    eff = effective_keyword_count(kws)
    verdict = classify_request(args.get("request") or "", None)
    rel = keyword_coverage(kws, entries)
    rel.update({
        "request_type": verdict["verdict"],
        "classification": verdict,
        "effective_keywords": eff,
        "threshold": 0.6,
        "needs_second_pass": bool(rel.get("coverage") is not None
                                  and rel["coverage"] < 0.6 and eff >= 2),
        "weak_validation": eff < 2,
    })
    if eff < 2:
        rel["weak_reason"] = f"only {eff} effective keyword(s); coverage recorded, not gated"
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
