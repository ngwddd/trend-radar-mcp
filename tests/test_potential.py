# -*- coding: utf-8 -*-
"""Potential-news branch: pure scoring tests + registration contract."""
import unittest
from datetime import datetime

from trend_radar_mcp.potential import build_candidates, fallback_candidates
from trend_radar_mcp.server import INSTRUCTIONS
from trend_radar_mcp.tools import HANDLERS, TOOLS


def entry(eid, source_key, source, *, bucket="watch", first_seen=None,
          url="https://example.com/x", also=None, promo=False):
    now = first_seen or datetime.now().isoformat(timespec="seconds")
    out = {
        "event_id": eid, "title": f"title {eid}", "url": url,
        "source": source, "source_key": source_key, "published": now,
        "first_seen": now, "times_seen": 1, "bucket": bucket,
        "truth_score": 0.8, "comm_score": 0.4, "also_seen_in": also or [],
    }
    if promo:
        out["promotional_signal"] = "brand"
    return out


class PotentialBuildTest(unittest.TestCase):
    def test_no_history_caps_score_and_flags(self):
        payload = {"run_id": "r1", "entries": [entry("e1", "crossref-nature", "Nature")]}
        out = build_candidates(payload, [], top=5)
        self.assertIn("no_history", out["meta"]["degraded"])
        self.assertTrue(out["candidates"])
        self.assertLessEqual(out["candidates"][0]["score"], 60)

    def test_velocity_comes_from_previous_runs_without_new_table(self):
        prev = {"run_id": "r0", "generated_at": datetime.now().isoformat(timespec="seconds"),
                "entries": [entry("e1", "crossref-nature", "Nature")]}
        cur = {"run_id": "r1", "entries": [entry("e1", "crossref-nature", "Nature",
                                                 also=["hn-top"])]}
        out = build_candidates(cur, [prev], top=5)
        sig = out["candidates"][0]["signals"]
        self.assertEqual(sig["new_sources"], 1)
        self.assertGreater(sig["velocity_per_hour"], 0)
        self.assertNotIn("no_history", out["meta"]["degraded"])

    def test_promotional_and_trend_only_are_excluded(self):
        payload = {"run_id": "r1", "entries": [
            entry("promo", "hn-top", "HN", promo=True),
            entry("trend", "weibo-hot", "微博热搜", bucket="calibration"),
            entry("ok", "crossref-nature", "Nature"),
        ]}
        out = build_candidates(payload, [], top=8)
        ids = [c["event_id"] for c in out["candidates"]]
        self.assertIn("ok", ids)
        self.assertNotIn("promo", ids)
        self.assertNotIn("trend", ids)

    def test_top_and_same_source_cap(self):
        entries = [entry(f"e{i}", "hn-top", "Hacker News") for i in range(10)]
        out = build_candidates({"run_id": "r1", "entries": entries}, [], top=8)
        self.assertLessEqual(len(out["candidates"]), 2)

    def test_fallback_never_raises(self):
        out = fallback_candidates(None, 5, "timeout")
        self.assertEqual(out["candidates"], [])
        self.assertIn("timeout", out["meta"]["degraded"])


class PotentialRegistrationTest(unittest.TestCase):
    def test_tool_is_registered(self):
        names = {t["name"] for t in TOOLS}
        self.assertIn("radar_potential", names)
        self.assertIn("radar_potential", HANDLERS)

    def test_no_hot_news_auto_rule_is_documented(self):
        self.assertIn("任何时间", INSTRUCTIONS)
        self.assertIn("不要问用户", INSTRUCTIONS)
        self.assertIn("只有论文", INSTRUCTIONS)

    def test_tool_schema_is_opt_in(self):
        tool = next(t for t in TOOLS if t["name"] == "radar_potential")
        self.assertEqual(tool["inputSchema"]["required"], ["query"])
        self.assertNotIn("potential", next(t for t in TOOLS
                                          if t["name"] == "radar_scan")["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
