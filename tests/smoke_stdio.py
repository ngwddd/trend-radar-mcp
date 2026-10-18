# -*- coding: utf-8 -*-
"""Minimal MCP stdio client used as a smoke test."""
import json
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
env = dict(os.environ, TREND_RADAR_HOME=r"C:\Users\ggg\Desktop\deepseek development\trend-radar-mcp\.state")
proc = subprocess.Popen([sys.executable, "-m", "trend_radar_mcp"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                        cwd=r"C:\Users\ggg\Desktop\deepseek development\trend-radar-mcp",
                        env=env)


def call(obj):
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line)


print("init:", call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05",
                                "capabilities": {}, "clientInfo": {"name": "smoke"}}})["result"]["serverInfo"])
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()
tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
print("tools:", [t["name"] for t in tools])
res = call({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})["result"]["resources"]
print("resources:", len(res), res[0]["uri"])
r = call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
          "params": {"name": "radar_check", "arguments": {
              "entries": [{"title": "AI 安全与对齐"}], "keywords": ["安全", "对齐"]}}})
out = json.loads(r["result"]["content"][0]["text"])
print("relevance:", out["relevance"]["coverage"])
r = call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
          "params": {"name": "radar_check", "arguments": {
              "entries": [{"title": "某地发生事故，3 人受伤"}],
              "keywords": ["事故"],
              "external": [{"title": "某地发生事故，3 人受伤", "source": "澎湃新闻",
                            "url": "http://x", "date": "2026-10-18",
                            "summary": "12 月 3 日，某市发生事故，3 人受伤，2 辆车受损。"}]}}})
print("scored:", r["result"]["content"][0]["text"][:240].replace("\n", " "))
proc.stdin.close()
proc.terminate()
err = proc.stderr.read()
print("stderr tail:", err[-300:])
