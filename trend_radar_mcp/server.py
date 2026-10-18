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
trend-radar v0.3：三工具，一次调用拿到可写材料。

1. 直接调 radar_scan(query="用户原话")。scope 自动判定：三类之一，判不进就 broad（42 源全开）。
   显式 domain 永远优先；返回 scope_source=auto / explicit / auto-broad 可审计。
2. radar_scan 已经内联 core/watch 的前 3 条摘要（briefs）。够写就直接写；
   不够再用 radar_pack(ids≤12, fields=["summary"]) 补 —— 不要整桶拉。
3. truth / comm 是引擎按固定权重算的；0.6 是闸门，borderline=true 的条目必须复核。
4. 分桶：core=可引用；watch=可信但没传开（不是噪声）；suspect=传得广但不可信；
   calibration=热榜，只校准传播度，永不作内容源。
5. 补搜条目走 radar_check(external=[...]) 打分；发布前用 radar_check(bucket="core") 量关联度，
   <0.6 必须换角度重查，不许直接下结论。
6. 写简报：每条带 URL 与日期；预印本不写已发表；厂商自述不算独立验证；
   覆盖披露必须含失败源、降级流、跳过的空源。
7. 预算：radar_scan ≈6–8 KB，radar_pack ≈3 KB。默认值够用就不要加参数。
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
         "description": "42 个源、三路分类、13 条已知死路", "mimeType": "application/json"},
        {"uri": "radar://history", "name": "state & source health",
         "description": "状态库统计 + 连续空源黑名单", "mimeType": "application/json"},
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
    if uri == "radar://history":
        store = Store()
        try:
            body = {"stats": store.stats(), "source_health": store.health()}
        finally:
            store.close()
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(body, ensure_ascii=False)}]}
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
