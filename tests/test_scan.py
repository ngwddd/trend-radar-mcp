# -*- coding: utf-8 -*-
"""End-to-end scan test with the network layer replaced by fixtures."""
import os
import shutil
import tempfile
import unittest
from datetime import datetime

from trend_radar_mcp import scan as S


LONG_SUMMARY = ("研究报告给出了方法与结果：样本量 1200 人，提升 12%，"
                "并明确说明了适用条件与随访时间，结论仅适用于该队列。")


def _entry(key, title, published=None, summary=None):
    return {
        "title": title, "url": f"https://example.com/{abs(hash(title))}",
        "summary": summary or LONG_SUMMARY, "published": published,
        "published_raw": published, "authors": ["A"], "source_key": key,
        "source_label": S.SOURCES[key]["label"],
    }


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["TREND_RADAR_HOME"] = self.tmp
        self.today = datetime.now().astimezone().isoformat()
        self._orig = S._fetch_one

        fixture = {
            "arxiv-ai": [_entry("arxiv-ai", "预印本论文 A", self.today)],
            "arxiv-rss-ai": [],
            "openai-news": [_entry("openai-news", "官方发布 B", self.today)],
            "crossref-natbiotech": [],
            "crossref-natmi": [_entry("crossref-natmi", "同行评审论文 C", self.today)],
            "crossref-natcomp": [],
            "toutiao-hot": [{"title": "热榜第一", "url": "https://example.com/hot",
                             "summary": "热度值 100", "published": None,
                             "published_raw": None, "authors": [],
                             "source_key": "toutiao-hot",
                             "source_label": "头条热榜", "trend_rank": 1}],
        }

        def fake(key, since, days, major_days, maxn, limiter, force=False):
            return {"key": key, "entries": list(fixture.get(key, [])),
                    "status": "ok", "detail": None, "seconds": 0.01,
                    "archive_truncated": 0, "sub_failed": [], "stale": None}

        S._fetch_one = fake

    def tearDown(self):
        S._fetch_one = self._orig
        os.environ.pop("TREND_RADAR_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_buckets_and_state(self):
        payload = S.scan("ai", streams=["authority", "trend"], days=3, request="AI", deep=True)
        buckets = {}
        for e in payload["entries"]:
            buckets.setdefault(e["bucket"], []).append(e)

        # peer-reviewed + truth ok + comm 0.30 -> single-source hard evidence
        self.assertTrue(buckets.get("core"), buckets)
        self.assertTrue(any("crossref-natmi" == e["source_key"]
                            for e in buckets["core"]))
        # official but not independent -> watch, never core
        self.assertTrue(any("openai-news" == e["source_key"]
                            for e in buckets.get("watch", [])))
        # preprint (0.578) sits below the gate
        self.assertTrue(any("arxiv-ai" == e["source_key"]
                            for e in buckets.get("discarded", [])))
        # hot board is a calibration signal, never content
        self.assertTrue(any("toutiao-hot" == e["source_key"]
                            for e in buckets.get("calibration", [])))

        # first sighting -> is_new true everywhere
        self.assertTrue(all(e["is_new"] for e in payload["entries"]))

        # second run sees the same events and keeps first_seen stable
        again = S.scan("ai", streams=["authority", "trend"], days=3, request="AI", deep=True)
        by_id = {e["event_id"]: e for e in payload["entries"]}
        changed = [e for e in again["entries"]
                   if e["event_id"] in by_id
                   and e["first_seen"] != by_id[e["event_id"]]["first_seen"]]
        self.assertEqual(changed, [])
        self.assertTrue(any(e["times_seen"] >= 1 for e in again["entries"]))

    def test_days_are_validated(self):
        with self.assertRaises(ValueError):
            S.scan("ai", days=0)
        with self.assertRaises(ValueError):
            S.scan("nope", days=3)

    def test_broad_scope_uses_the_whole_registry(self):
        payload = S.scan(None, days=3, request="今天有什么")
        self.assertEqual(payload["scope"], "broad")
        self.assertEqual(payload["stats"]["requested_sources"], 41)
        self.assertEqual(payload["stats"].get("skipped_slow"), 1)
        self.assertEqual(payload["relevance"]["mode"], "broad")

    def test_stream_with_no_source_falls_back(self):
        # social has no authority tier at all; an empty run would be a bug.
        payload = S.scan("social", streams=["authority"], days=3)
        self.assertGreater(payload["stats"]["requested_sources"], 0)
        self.assertTrue(payload["stream_fallback"])


if __name__ == "__main__":
    unittest.main()
