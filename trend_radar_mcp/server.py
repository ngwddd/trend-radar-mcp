"""stdio JSON-RPC 2.0 server (MCP), no SDK dependency.

stdout carries protocol frames only. Every diagnostic goes to stderr.

Why hand-rolled: the engine is Python, the client spawns it as a subprocess,
and a 300-line transport is easier to audit (and to keep working on Windows)
than a dependency that changes shape between MCP revisions.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from . import __version__
from .registry import BLOCKED, DOMAINS, SOURCES
from .store import Store
from .tools import HANDLERS, TOOLS

SERVER_NAME = "trend-radar"
PROTOCOL_VERSION = "2024-11-05"

INSTRUCTIONS = """\
trend-radar: 三路情报采集与研判（digest 优先，正文按需取）。

1. 先判 scope：social / science / ai 三选一；**判不了就用 broad**（42 源全开），不要猜。
   broad 比单类慢约 1 倍、贵约 1 倍，只在真的跨类或用户要「全面搜」时用。
2. radar_scan 返回的是 digest：id、标题、truth/comm、来源、日期、new、flag。
   它**不含摘要正文**。要写哪几条，再用 radar_entries(ids=[...], fields=['summary'])
   只取那几条 —— 不要整桶拉、不要先拉全量再筛。
3. truth / comm 是引擎按固定权重算的，0.6 是闸门；不要自己估分，也不要把两个分合成一个。
4. 分桶：core=双项达标或单源硬证据；watch=可信但没传开（不是噪声）；
   suspect=传得广但不可信；calibration=热榜，只校准传播度，永不作内容源。
5. relevance.needs_second_pass=true 时不许直接下结论：换角度/关键词重查，再复核权重。
6. 写简报：每条带 URL 与日期；预印本不写已发表；厂商自述不算独立验证；
   「本次覆盖」如实写失败/降级的源与 skipped_empty 的源。
7. 预算：radar_scan ≈5 KB、radar_entries ≈3 KB。摘要默认 300 字，够用就别加大。
"""


def log(*args) -> None:
    print("[trend-radar]", *args, file=sys.stderr, flush=True)


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _registry_doc() -> dict:
    return {
        "domains": DOMAINS,
        "counts": {k: sum(1 for s in SOURCES.values() if s.get("class") == k)
                   for k in ("authority", "social", "trend")},
        "sources": {k: {kk: vv for kk, vv in v.items() if kk != "note"}
                    for k, v in SOURCES.items()},
        "blocked": [{"url": u, "reason": r} for u, r in BLOCKED],
    }


def _resource_list() -> list[dict]:
    out = [
        {"uri": "radar://registry", "name": "source registry",
         "description": "42 个源、三路分类、权重归属与已知死路", "mimeType": "application/json"},
        {"uri": "radar://runs", "name": "recent runs",
         "description": "最近的扫描记录（run_id / 时间 / 统计）", "mimeType": "application/json"},
    ]
    store = Store()
    try:
        for run in store.recent_runs(limit=10):
            out.append({
                "uri": f"radar://runs/{run['run_id']}",
                "name": f"run {run['run_id']}",
                "description": f"{run.get('domain')} · {run.get('created_at')} · "
                               f"{(run.get('stats') or {}).get('emitted', '?')} 条",
                "mimeType": "application/json",
            })
    finally:
        store.close()
    return out


def _read_resource(uri: str) -> dict:
    if uri == "radar://registry":
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(_registry_doc(), ensure_ascii=False)}]}
    if uri == "radar://runs":
        store = Store()
        try:
            runs = store.recent_runs(limit=20)
        finally:
            store.close()
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(runs, ensure_ascii=False)}]}
    if uri.startswith("radar://runs/"):
        rest = uri[len("radar://runs/"):]
        run_id, _, tail = rest.partition("/")
        store = Store()
        try:
            run = store.get_run(run_id)
        finally:
            store.close()
        if not run:
            raise ValueError(f"no such run: {run_id}")
        payload = run.get("payload") or {}
        if tail == "entries":
            body = {"run_id": run_id, "entries": payload.get("entries") or []}
        else:
            body = {k: v for k, v in payload.items() if k != "entries"}
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(body, ensure_ascii=False)}]}
    raise ValueError(f"unknown resource: {uri}")


def handle(req: dict) -> dict | None:
    rid = req.get("id")
    method = req.get("method")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }}
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        try:
            result = fn(args)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False, indent=1)}],
                "isError": False,
            }}
        except Exception as exc:  # noqa: BLE001 - report, never kill the server
            log("tool error:", traceback.format_exc())
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": f"ERROR: {type(exc).__name__}: {exc}"}],
                "isError": True,
            }}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": _resource_list()}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resourceTemplates": [
            {"uriTemplate": "radar://runs/{run_id}", "name": "run manifest",
             "description": "一次扫描的统计、覆盖、关联度（不含条目）",
             "mimeType": "application/json"},
            {"uriTemplate": "radar://runs/{run_id}/entries", "name": "run entries",
             "description": "一次扫描的全部条目标（compact JSON）",
             "mimeType": "application/json"},
        ]}}
    if method == "resources/read":
        uri = (req.get("params") or {}).get("uri") or ""
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": _read_resource(uri)}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32002, "message": str(exc)}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def main() -> None:
    for stream in (sys.stdout, sys.stdin, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    log(f"started pid={os.getpid()} version={__version__}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("bad json:", line[:200])
            continue
        try:
            resp = handle(req)
        except Exception:  # noqa: BLE001 - a malformed request must not kill stdio
            log("handler crash:", traceback.format_exc())
            rid = req.get("id") if isinstance(req, dict) else None
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": "internal error"}}
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()
