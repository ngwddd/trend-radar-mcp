# -*- coding: utf-8 -*-
"""Unit tests for the ported scoring core. No network, no state."""
import unittest

from trend_radar_mcp.dedupe import event_id, is_same_event, title_tokens
from trend_radar_mcp.registry import (EVIDENCE_WEIGHT, SOURCES, SOURCE_WEIGHT,
                                      evidence_type_for, identity_of)
from trend_radar_mcp.relevance import keyword_coverage
from trend_radar_mcp.scoring import (comm_weight, truth_weight,
                                     truth_weight_social)


class RegistryTest(unittest.TestCase):
    def test_every_source_has_a_weight(self):
        self.assertEqual(len(SOURCES), 42)
        missing = [k for k in SOURCES if k not in SOURCE_WEIGHT]
        self.assertEqual(missing, [])

    def test_identities_collapse_only_real_duplicates(self):
        self.assertEqual(identity_of("hn-frontpage"), identity_of("hn-top"))
        self.assertEqual(identity_of("arxiv-ai"), identity_of("arxiv-rss-ai"))
        # Two different outlets that merely share a weight bucket must NOT merge.
        self.assertNotEqual(identity_of("新京报"), identity_of("南方都市报"))
        self.assertEqual(identity_of("央视新闻"), identity_of("中央电视台"))
        self.assertEqual(identity_of("上游新闻客户端"), identity_of("上游新闻"))

    def test_front_matter_is_not_peer_reviewed(self):
        entry = {"title": "A book review", "doi": "10.1038/d41586-026-03217-6",
                 "authors": ["A"]}
        self.assertEqual(evidence_type_for("crossref-nature", "authority", entry),
                         "journalism")
        paper = {"title": "A real study", "doi": "10.1038/s41586-026-0001-1",
                 "authors": ["A", "B"], "abstract_missing": False}
        self.assertEqual(evidence_type_for("crossref-nature", "authority", paper),
                         "peer-reviewed")

    def test_evidence_weights_are_ordered(self):
        self.assertGreater(EVIDENCE_WEIGHT["peer-reviewed"],
                           EVIDENCE_WEIGHT["preprint"])
        self.assertGreater(EVIDENCE_WEIGHT["preprint"],
                           EVIDENCE_WEIGHT["journalism"])


class ScoringTest(unittest.TestCase):
    def test_peer_reviewed_clears_truth_gate_even_when_single_source(self):
        entry = {"title": "A study", "summary": "methods and results"}
        self.assertGreaterEqual(truth_weight("crossref-nature", "authority",
                                             entry=entry), 0.6)
        self.assertEqual(comm_weight(1, False), 0.30)
        self.assertLess(comm_weight(1, False), 0.6)

    def test_second_independent_source_clears_comm(self):
        self.assertGreaterEqual(comm_weight(2, False), 0.6)

    def test_comm_weights_quality_not_just_count(self):
        from trend_radar_mcp.scoring import comm_weight_eff
        self.assertEqual(comm_weight_eff([]), 0.30)          # single source
        self.assertGreaterEqual(comm_weight_eff([0.60]), 0.60)  # average pickup passes
        self.assertLess(comm_weight_eff([0.45]), 0.60)       # one low-tier reprint does NOT
        self.assertGreater(comm_weight_eff([0.90]),
                           comm_weight_eff([0.45]))           # top-tier pickup counts more
        self.assertGreater(comm_weight_eff([0.6, 0.6]),
                           comm_weight_eff([0.6]))

    def test_trend_board_is_capped_below_truth_gate(self):
        entry = {"title": "热搜第一", "source_key": "toutiao-hot", "is_trend": True}
        self.assertLess(truth_weight_social(entry), 0.6)
        self.assertTrue(entry.get("trend_capped"))

    def test_social_official_notice_beats_unattributed(self):
        base = {"source_label": "澎湃新闻"}
        official = dict(base, title="某市发布通报", summary="官方通报：3 人受伤")
        vague = dict(base, title="听说有件事", summary="大家都在传")
        self.assertGreater(truth_weight_social(official),
                           truth_weight_social(vague))


class DedupeTest(unittest.TestCase):
    def test_same_event_different_titles(self):
        a = title_tokens("Nature 发表新研究：某模型提升 12%")
        b = title_tokens("Nature：某模型提升 12%（新研究）")
        self.assertTrue(is_same_event(a, b))
        self.assertEqual(event_id("同一标题"), event_id("同一标题"))

    def test_different_events_do_not_merge(self):
        a = title_tokens("某地发生交通事故")
        b = title_tokens("某公司发布新款手机")
        self.assertFalse(is_same_event(a, b))


class RelevanceTest(unittest.TestCase):
    def test_coverage_is_directional(self):
        entries = [{"title": "AI 安全与对齐", "summary": "风险研究"}]
        rel = keyword_coverage(["安全", "对齐", "风险"], entries)
        self.assertEqual(rel["coverage"], 1.0)
        # A generic AI paper mentions "AI" but not the intent terms.
        generic = [{"title": "通用 AI 模型评测", "summary": "benchmark"}]
        self.assertEqual(keyword_coverage(["安全", "对齐", "风险"], generic)["coverage"], 0.0)


if __name__ == "__main__":
    unittest.main()
