from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from pipeline.report_m1_health import (
    DEFAULT_MAX_GAP_HOURS,
    ROOT,
    RunRecord,
    build_report,
)


class M1HealthReportTests(unittest.TestCase):
    def test_github_actions_cadence_gap_under_default_threshold_passes(self) -> None:
        now = _time("2026-06-23T13:54:34Z")
        runs = _clean_runs(now, hours_before=[80, 73, 66.08, 59.16, 52.24, 45.32])
        runs.extend(_clean_runs(now, hours_before=[38.4, 31.48, 24.56, 17.64, 10.72, 3.8, 0.1]))

        report = build_report(
            runs=runs,
            now=now,
            window_hours=72.0,
            min_health=0.8,
            max_gap_hours=DEFAULT_MAX_GAP_HOURS,
        )

        self.assertEqual(report["criteria"]["max_gap_hours"], 8.0)
        self.assertEqual(report["current_clean_streak"]["max_gap_hours"], 6.92)
        self.assertEqual(report["verdict"]["status"], "pass")

    def test_strict_three_hour_override_still_flags_the_same_gap(self) -> None:
        now = _time("2026-06-23T13:54:34Z")
        runs = _clean_runs(now, hours_before=[80, 73, 66.08, 59.16, 52.24, 45.32])
        runs.extend(_clean_runs(now, hours_before=[38.4, 31.48, 24.56, 17.64, 10.72, 3.8, 0.1]))

        report = build_report(
            runs=runs,
            now=now,
            window_hours=72.0,
            min_health=0.8,
            max_gap_hours=3.0,
        )

        self.assertEqual(report["verdict"]["status"], "fail")
        self.assertIn("exceeds the 3.00h tolerance", report["verdict"]["reason"])

    def test_gap_above_default_threshold_still_fails(self) -> None:
        now = _time("2026-06-23T13:54:34Z")
        runs = _clean_runs(now, hours_before=[80, 71, 62, 53, 44, 35, 26, 17, 8, 0.1])

        report = build_report(
            runs=runs,
            now=now,
            window_hours=72.0,
            min_health=0.8,
            max_gap_hours=DEFAULT_MAX_GAP_HOURS,
        )

        self.assertEqual(report["current_clean_streak"]["max_gap_hours"], 9.0)
        self.assertEqual(report["verdict"]["status"], "fail")
        self.assertIn("exceeds the 8.00h tolerance", report["verdict"]["reason"])


def _clean_runs(now: datetime, hours_before: list[float]) -> list[RunRecord]:
    return [_run(now - timedelta(hours=hours)) for hours in hours_before]


def _run(started_at: datetime) -> RunRecord:
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ-live")
    return RunRecord(
        path=ROOT / "data" / "manifests" / "2026-06" / f"{run_id}.json",
        run_id=run_id,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=10),
        status="success",
        total_new_items=100,
        include_total=10,
        healthy_include=9,
        health_ratio=0.9,
        gate_passed=True,
        source_results=[],
        github_actions=True,
    )


class DemotionCandidateTests(unittest.TestCase):
    def test_persistent_zero_item_source_is_candidate(self) -> None:
        now = _time("2026-06-30T00:00:00Z")
        runs = [_run_with_source(now, hours) for hours in range(6)]  # 6 runs, all zero
        report = build_report(
            runs=runs, now=now, window_hours=72.0, min_health=0.8, max_gap_hours=8.0
        )
        candidates = report["demotion_candidates"]
        arxiv = [c for c in candidates if c["source_id"] == "arxiv-cs-cl"]
        self.assertTrue(arxiv)
        self.assertTrue(arxiv[0]["is_candidate"])
        self.assertEqual(arxiv[0]["zero_ratio"], 1.0)
        self.assertEqual(arxiv[0]["seen_runs"], 6)

    def test_below_min_samples_not_candidate_but_listed(self) -> None:
        now = _time("2026-06-30T00:00:00Z")
        runs = [_run_with_source(now, hours) for hours in range(3)]  # 3 < min_samples 5
        report = build_report(
            runs=runs, now=now, window_hours=72.0, min_health=0.8, max_gap_hours=8.0
        )
        arxiv = [c for c in report["demotion_candidates"] if c["source_id"] == "arxiv-cs-cl"]
        self.assertTrue(arxiv)
        self.assertFalse(arxiv[0]["is_candidate"])
        self.assertEqual(arxiv[0]["zero_ratio"], 1.0)

    def test_low_zero_ratio_not_candidate(self) -> None:
        now = _time("2026-06-30T00:00:00Z")
        runs = [
            _run_with_source(now, hours, item_count=0 if hours % 3 == 0 else 5)
            for hours in range(6)
        ]
        report = build_report(
            runs=runs, now=now, window_hours=72.0, min_health=0.8, max_gap_hours=8.0
        )
        arxiv = [c for c in report["demotion_candidates"] if c["source_id"] == "arxiv-cs-cl"]
        self.assertTrue(arxiv)
        self.assertFalse(arxiv[0]["is_candidate"])
        self.assertEqual(arxiv[0]["zero_ratio"], round(2 / 6, 4))

    def test_healthy_source_not_listed(self) -> None:
        now = _time("2026-06-30T00:00:00Z")
        runs = [_run_with_source(now, hours, item_count=5) for hours in range(6)]
        report = build_report(
            runs=runs, now=now, window_hours=72.0, min_health=0.8, max_gap_hours=8.0
        )
        arxiv = [c for c in report["demotion_candidates"] if c["source_id"] == "arxiv-cs-cl"]
        self.assertFalse(arxiv)  # zero_runs == 0 → not listed


def _run_with_source(
    now: datetime,
    hours_before: int,
    *,
    item_count: int = 0,
    source_id: str = "arxiv-cs-cl",
) -> RunRecord:
    started = now - timedelta(hours=hours_before)
    run_id = started.strftime("%Y%m%dT%H%M%SZ-live")
    return RunRecord(
        path=ROOT / "data" / "manifests" / "2026-06" / f"{run_id}.json",
        run_id=run_id,
        started_at=started,
        finished_at=started + timedelta(seconds=10),
        status="success",
        total_new_items=100,
        include_total=10,
        healthy_include=9,
        health_ratio=0.9,
        gate_passed=True,
        source_results=[
            {
                "source_id": source_id,
                "m1_action": "include",
                "status": "success",
                "item_count": item_count,
                "error": None,
            }
        ],
        github_actions=True,
    )


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


if __name__ == "__main__":
    unittest.main()
