# -*- coding: utf-8 -*-
"""Protocol-level tests: the server must answer MCP frames without a client SDK."""
import json
import os
import tempfile
import unittest

from trend_radar_mcp import server


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["TREND_RADAR_HOME"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("TREND_RADAR_HOME", None)

    def test_initialize(self):
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "trend-radar")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertIn("instructions", resp["result"])

    def test_tools_list_has_schemas(self):
        resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"radar_scan", "radar_pack", "radar_check", "radar_potential"})
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_unknown_method_is_an_error(self):
        resp = server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_registry_resource_reads(self):
        resp = server.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                              "params": {"uri": "radar://registry"}})
        body = json.loads(resp["result"]["contents"][0]["text"])
        self.assertEqual(len(body["sources"]), 42)

    def test_tool_call_is_json_text(self):
        resp = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "radar_check",
                                         "arguments": {"entries": [{"title": "AI 安全与对齐"}],
                                                       "keywords": ["安全", "对齐"]}}})
        self.assertFalse(resp["result"]["isError"])
        body = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("relevance", body)

    def test_bad_tool_returns_iserror_not_crash(self):
        resp = server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "radar_pack",
                                         "arguments": {"run_id": "missing"}}})
        self.assertTrue(resp["result"]["isError"])

    def test_notifications_get_no_response(self):
        self.assertIsNone(server.handle({"jsonrpc": "2.0",
                                         "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
