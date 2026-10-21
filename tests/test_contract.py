# -*- coding: utf-8 -*-
"""Contract: the default radar_scan response exposes no potential fields."""
import unittest
from unittest.mock import patch

from trend_radar_mcp.tools import h_scan


def _payload():
    return {
        "run_id": "r1",
        "scope": "broad",
        "domain": "broad",
        "window": {"days": 1, "major_days": 0,
                   "since": "2026-10-21", "until": "2026-10-21"},
        "stats": {
            "requested_sources": 42, "succeeded_sources": 41, "failed_sources": 1,
            "skipped_empty_sources": 0, "skipped_slow": 0,
            "emitted": 1, "wall_seconds": 0.1,
            "buckets": {"core": 0, "watch": 1, "suspect": 0,
                        "calibration": 0, "discarded": 0},
        },
        "coverage": {
            "sources_report": [],
            "degraded_streams": [],
            "stale_feeds": {},
            "sub_feed_failures": {},
            "archive_truncated": False,
            "source_funnel": {},
        },
        "relevance": {},
        "entries": [{
            "event_id": "e1", "title": "t", "url": "https://example.com/a",
            "source": "Nature", "source_key": "crossref-nature",
            "published": "2026-10-21", "first_seen": "2026-10-21T08:00:00",
            "times_seen": 1, "bucket": "watch", "truth_score": 0.9,
            "comm_score": 0.4, "also_seen_in": [], "topic_hits": 1,
        }],
    }


class ScanContractTest(unittest.TestCase):
    def test_scan_has_no_potential_fields(self):
        with patch("trend_radar_mcp.tools.scan", return_value=_payload()):
            out = h_scan({"query": "今天热点", "domain": "broad", "briefs": 0})
        self.assertNotIn("candidates", out)
        self.assertNotIn("potential", out)

    def test_empty_core_suggests_potential(self):
        with patch("trend_radar_mcp.tools.scan", return_value=_payload()):
            out = h_scan({"query": "今天热点", "domain": "broad", "briefs": 0})
        self.assertIn("radar_potential", out["notice"])

    def test_core_only_papers_suggests_potential(self):
        payload = _payload()
        payload["entries"] = [{
            "event_id": "p1", "title": "A paper", "url": "https://doi.org/x",
            "source": "Nature", "source_key": "crossref-nature",
            "published": "", "first_seen": "2026-10-21T08:00:00",
            "times_seen": 1, "bucket": "core", "truth_score": 0.9,
            "comm_score": 0.3, "also_seen_in": [], "topic_hits": 1,
            "single_source_hard_evidence": True,
        }]
        with patch("trend_radar_mcp.tools.scan", return_value=payload):
            out = h_scan({"query": "今天热点", "domain": "broad", "briefs": 0})
        self.assertIn("只有论文", out["notice"])

    def test_hot_core_has_no_suggestion(self):
        payload = _payload()
        payload["entries"][0]["bucket"] = "core"
        payload["entries"][0]["published"] = "2026-10-21T10:00:00"
        with patch("trend_radar_mcp.tools.scan", return_value=payload):
            out = h_scan({"query": "今天热点", "domain": "broad", "briefs": 0})
        self.assertEqual(out["notice"], "")


if __name__ == "__main__":
    unittest.main()
