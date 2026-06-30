"""Offline unit tests for pipeline.report_echo.

No network. Deterministic. Built on synthetic cluster fixtures, not live portal data.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import report_echo


def _cluster(
    cid: str,
    source_count: int,
    tier_mix: dict[str, int],
    *,
    titles: list[str] | None = None,
    rank: int | None = None,
    source_names: list[str] | None = None,
) -> dict:
    return {
        "id": cid,
        "title": cid,
        "rank": rank,
        "source_count": source_count,
        "source_names": source_names or [],
        "tier_mix": tier_mix,
        "item_titles": titles or [],
    }


class ClassifyClusterTest(unittest.TestCase):
    def test_pure_tier3_multi_source_is_repost_dominated(self) -> None:
        row = report_echo.classify_cluster(
            _cluster("c1", 2, {"tier1": 0, "tier2": 0, "tier3": 2, "tier4": 0})
        )
        self.assertTrue(row["scorable"])
        self.assertTrue(row["repost_dominated"])
        self.assertEqual(row["independent_classes"], 0)

    def test_tier1_anchor_not_repost(self) -> None:
        row = report_echo.classify_cluster(
            _cluster("c2", 2, {"tier1": 1, "tier2": 0, "tier3": 1, "tier4": 0})
        )
        self.assertFalse(row["repost_dominated"])
        self.assertEqual(row["independent_classes"], 1)

    def test_tier4_counts_as_independent_anchor(self) -> None:
        row = report_echo.classify_cluster(
            _cluster("c3", 2, {"tier1": 0, "tier2": 0, "tier3": 1, "tier4": 1})
        )
        self.assertFalse(row["repost_dominated"])
        self.assertEqual(row["independent_classes"], 1)

    def test_single_source_not_scorable(self) -> None:
        row = report_echo.classify_cluster(_cluster("c4", 1, {"tier3": 1}))
        self.assertFalse(row["scorable"])
        self.assertFalse(row["repost_dominated"])
        self.assertIsNone(row["independent_classes"])

    def test_missing_tier_mix_not_scorable(self) -> None:
        row = report_echo.classify_cluster({"id": "c5", "source_count": 2})
        self.assertFalse(row["scorable"])
        self.assertFalse(row["repost_dominated"])

    def test_all_zero_tier_mix_not_scorable(self) -> None:
        row = report_echo.classify_cluster(
            _cluster("c6", 2, {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0})
        )
        self.assertFalse(row["scorable"])


class BuildReportTest(unittest.TestCase):
    def test_ratio_and_mean_over_scorable_only(self) -> None:
        portal = {
            "clusters": [
                _cluster("a", 2, {"tier1": 0, "tier2": 0, "tier3": 2, "tier4": 0}),
                _cluster("b", 2, {"tier1": 1, "tier2": 0, "tier3": 1, "tier4": 0}),
                _cluster("c", 1, {"tier3": 1}),
            ]
        }
        report = report_echo.build_echo_report(portal, top_n=10)
        self.assertEqual(report["repost_dominated_ratio"], 0.5)
        self.assertEqual(report["mean_independent_classes"], 0.5)
        self.assertEqual(len(report["repost_dominated_clusters"]), 1)
        self.assertEqual(report["criteria"]["scorable"], 2)
        self.assertEqual(report["criteria"]["single_source_in_top"], 1)

    def test_top_n_limits_selected(self) -> None:
        portal = {"clusters": [_cluster(f"c{i}", 2, {"tier3": 2}) for i in range(15)]}
        report = report_echo.build_echo_report(portal, top_n=5)
        self.assertEqual(report["criteria"]["selected"], 5)
        self.assertEqual(report["criteria"]["scorable"], 5)
        self.assertEqual(report["repost_dominated_ratio"], 1.0)

    def test_no_clusters_yields_none_metrics(self) -> None:
        report = report_echo.build_echo_report({"clusters": []}, top_n=10)
        self.assertIsNone(report["repost_dominated_ratio"])
        self.assertIsNone(report["mean_independent_classes"])
        self.assertEqual(report["repost_dominated_clusters"], [])

    def test_near_duplicate_titles_score_high_similarity(self) -> None:
        row = report_echo.classify_cluster(
            _cluster(
                "c",
                2,
                {"tier3": 2},
                titles=["OpenAI launches new model", "OpenAI launches new model!"],
            )
        )
        self.assertIsNotNone(row["title_similarity"])
        self.assertGreater(row["title_similarity"], 0.8)


class CLITest(unittest.TestCase):
    def test_main_json_format_returns_zero(self) -> None:
        portal = {
            "generated_at": "2026-06-30T00:00:00Z",
            "clusters": [_cluster("a", 2, {"tier3": 2})],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "portal.json"
            path.write_text(json.dumps(portal), encoding="utf-8")
            rc = report_echo.main(["--portal", str(path), "--format", "json"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
