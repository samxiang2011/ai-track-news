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


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


if __name__ == "__main__":
    unittest.main()
