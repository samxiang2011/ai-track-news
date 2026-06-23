from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_ROOT = ROOT / "data" / "manifests"
DEFAULT_WINDOW_HOURS = 72.0
DEFAULT_MIN_HEALTH = 0.8
DEFAULT_MAX_GAP_HOURS = 8.0


@dataclass(frozen=True)
class RunRecord:
    path: Path
    run_id: str
    started_at: datetime
    finished_at: datetime
    status: str
    total_new_items: int
    include_total: int
    healthy_include: int
    health_ratio: float
    gate_passed: bool
    source_results: list[dict[str, Any]]
    github_actions: bool | None

    def is_clean(self, min_health: float) -> bool:
        return (
            self.status != "failed"
            and self.include_total > 0
            and self.health_ratio >= min_health
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    now = _parse_cli_time(args.now) if args.now else datetime.now(timezone.utc)

    runs = load_runs(args.manifest_root, include_dry_runs=args.include_dry_runs)
    report = build_report(
        runs=runs,
        now=now,
        window_hours=args.window_hours,
        min_health=args.min_health,
        max_gap_hours=args.max_gap_hours,
    )

    markdown = format_markdown(report)
    if args.github_summary:
        wrote_summary = write_github_summary(markdown)
        report["github_summary_written"] = wrote_summary

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(markdown)

    if args.strict and report["verdict"]["status"] == "fail":
        return 1
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report M1 source-health acceptance status.")
    parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--min-health", type=float, default=DEFAULT_MIN_HEALTH)
    parser.add_argument("--max-gap-hours", type=float, default=DEFAULT_MAX_GAP_HOURS)
    parser.add_argument("--now", help="Override current UTC time for deterministic checks.")
    parser.add_argument("--include-dry-runs", action="store_true")
    parser.add_argument("--github-summary", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero only when verdict is fail.",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args(argv)


def load_runs(manifest_root: Path, include_dry_runs: bool = False) -> list[RunRecord]:
    runs: list[RunRecord] = []
    for path in sorted(manifest_root.rglob("*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            record = parse_run(path, manifest)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            continue
        if include_dry_runs or record.run_id.endswith("-live"):
            runs.append(record)
    return sorted(runs, key=lambda run: run.started_at)


def parse_run(path: Path, manifest: dict[str, Any]) -> RunRecord:
    run_id = _require_str(manifest.get("run_id"), "run_id")
    started_at = _require_time(manifest.get("started_at"), "started_at")
    finished_at = _parse_time(manifest.get("finished_at")) or started_at
    source_results = _as_source_results(manifest.get("source_results"))
    health = manifest.get("source_health")
    if not isinstance(health, dict):
        health = {}

    include_total = _as_int(health.get("include_total"))
    healthy_include = _as_int(health.get("healthy_include"))
    health_ratio = _as_float(health.get("health_ratio"))

    if include_total is None or healthy_include is None or health_ratio is None:
        include_rows = [row for row in source_results if row.get("m1_action") == "include"]
        include_total = len(include_rows)
        healthy_include = sum(1 for row in include_rows if _source_row_is_healthy(row))
        health_ratio = healthy_include / include_total if include_total else 0.0

    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    github_actions = runtime.get("github_actions")
    if not isinstance(github_actions, bool):
        github_actions = None

    return RunRecord(
        path=path,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        status=str(manifest.get("status") or "unknown"),
        total_new_items=_as_int(manifest.get("total_new_items")) or 0,
        include_total=include_total,
        healthy_include=healthy_include,
        health_ratio=health_ratio,
        gate_passed=bool(health.get("gate_passed", health_ratio >= DEFAULT_MIN_HEALTH)),
        source_results=source_results,
        github_actions=github_actions,
    )


def build_report(
    runs: list[RunRecord],
    now: datetime,
    window_hours: float,
    min_health: float,
    max_gap_hours: float,
) -> dict[str, Any]:
    now = _ensure_utc(now)
    window_start = now - timedelta(hours=window_hours)
    eligible_runs = [run for run in runs if run.started_at <= now + timedelta(minutes=5)]
    recent_runs = [run for run in eligible_runs if run.started_at >= window_start]
    latest = eligible_runs[-1] if eligible_runs else None
    clean_streak = _trailing_clean_streak(eligible_runs, min_health)
    clean_since = clean_streak[0].started_at if clean_streak else None
    clean_hours = _hours_between(clean_since, now) if clean_since else 0.0
    gap_runs = _gap_relevant_runs(clean_streak, window_start)
    max_gap = _max_gap_hours(gap_runs)

    verdict, reason = _verdict(
        latest=latest,
        recent_runs=recent_runs,
        clean_hours=clean_hours,
        max_gap=max_gap,
        window_hours=window_hours,
        min_health=min_health,
        max_gap_hours=max_gap_hours,
    )

    source_failures = _source_failure_ranking(recent_runs)
    latest_payload = _run_payload(latest) if latest else None
    success_count = sum(1 for run in recent_runs if run.status == "success")
    clean_count = sum(1 for run in recent_runs if run.is_clean(min_health))
    github_count = sum(1 for run in recent_runs if run.github_actions is True)
    local_count = sum(1 for run in recent_runs if run.github_actions is False)
    unknown_runner_count = sum(1 for run in recent_runs if run.github_actions is None)
    health_values = [run.health_ratio for run in recent_runs]

    return {
        "generated_at": _iso(now),
        "criteria": {
            "window_hours": window_hours,
            "min_health": min_health,
            "max_gap_hours": max_gap_hours,
        },
        "window": {
            "started_at": _iso(window_start),
            "ended_at": _iso(now),
            "live_runs": len(recent_runs),
            "status_success_runs": success_count,
            "status_success_rate": _ratio(success_count, len(recent_runs)),
            "clean_runs": clean_count,
            "clean_run_rate": _ratio(clean_count, len(recent_runs)),
            "github_actions_runs": github_count,
            "local_runs": local_count,
            "unknown_runner_runs": unknown_runner_count,
            "average_health_ratio": _average(health_values),
            "minimum_health_ratio": min(health_values) if health_values else None,
            "total_new_items": sum(run.total_new_items for run in recent_runs),
        },
        "current_clean_streak": {
            "started_at": _iso(clean_since) if clean_since else None,
            "hours": round(clean_hours, 2),
            "runs": len(clean_streak),
            "runs_in_window": sum(1 for run in clean_streak if run.started_at >= window_start),
            "max_gap_hours": round(max_gap, 2),
        },
        "latest_run": latest_payload,
        "include_source_failures": source_failures,
        "verdict": {
            "status": verdict,
            "reason": reason,
        },
    }


def format_markdown(report: dict[str, Any]) -> str:
    verdict = report["verdict"]
    criteria = report["criteria"]
    window = report["window"]
    streak = report["current_clean_streak"]
    latest = report["latest_run"]

    lines = [
        "# M1 Health Acceptance Report",
        "",
        f"**Verdict:** `{verdict['status']}`",
        "",
        verdict["reason"],
        "",
        "## Criteria",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Lookback window | {_hours(criteria['window_hours'])} |",
        f"| Minimum include-source health | {_percent(criteria['min_health'])} |",
        f"| Maximum accepted run gap | {_hours(criteria['max_gap_hours'])} |",
        "",
        "## Window",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Window start | `{window['started_at']}` |",
        f"| Window end | `{window['ended_at']}` |",
        f"| Live runs | {window['live_runs']} |",
        (
            "| Status success rate | "
            f"{_count_rate(window['status_success_runs'], window['live_runs'])} |"
        ),
        f"| Clean run rate | {_count_rate(window['clean_runs'], window['live_runs'])} |",
        f"| Known GitHub Actions runs | {window['github_actions_runs']} |",
        f"| Runner metadata unknown | {window['unknown_runner_runs']} |",
        f"| Average health | {_optional_percent(window['average_health_ratio'])} |",
        f"| Minimum health | {_optional_percent(window['minimum_health_ratio'])} |",
        f"| Deduped items | {window['total_new_items']} |",
        "",
        "## Current Clean Streak",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Started at | {_code_or_dash(streak['started_at'])} |",
        f"| Duration | {_hours(streak['hours'])} |",
        f"| Runs | {streak['runs']} |",
        f"| Runs in window | {streak['runs_in_window']} |",
        f"| Maximum run gap | {_hours(streak['max_gap_hours'])} |",
    ]

    if latest:
        lines.extend(
            [
                "",
                "## Latest Run",
                "",
                "| Metric | Value |",
                "| --- | ---: |",
                f"| Run id | `{latest['run_id']}` |",
                f"| Status | `{latest['status']}` |",
                f"| Health | {_percent(latest['health_ratio'])} |",
                f"| Include sources | {latest['healthy_include']}/{latest['include_total']} |",
                f"| Deduped items | {latest['total_new_items']} |",
                f"| GitHub Actions runtime | {_runtime_label(latest['github_actions'])} |",
                f"| Manifest | `{latest['path']}` |",
            ]
        )

    failures = report["include_source_failures"]
    lines.extend(["", "## Include Source Failures", ""])
    if not failures:
        lines.append("No unhealthy include-source observations in the lookback window.")
    else:
        lines.extend(
            [
                "| Source | Bad runs | Last status | Last item count | Last error |",
                "| --- | ---: | --- | ---: | --- |",
            ]
        )
        for row in failures[:10]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{_escape_table(row['source_id'])}`",
                        str(row["bad_runs"]),
                        f"`{_escape_table(row['last_status'])}`",
                        str(row["last_item_count"]),
                        _escape_table(row["last_error"] or "-"),
                    ]
                )
                + " |"
            )

    return "\n".join(lines)


def write_github_summary(markdown: str) -> bool:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return False
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write(markdown)
        handle.write("\n")
    return True


def _verdict(
    latest: RunRecord | None,
    recent_runs: list[RunRecord],
    clean_hours: float,
    max_gap: float,
    window_hours: float,
    min_health: float,
    max_gap_hours: float,
) -> tuple[str, str]:
    if latest is None:
        return "pending", "No live manifests are available yet."
    if not recent_runs:
        return "pending", "No live M1 runs were found in the lookback window."
    if not latest.is_clean(min_health):
        return (
            "fail",
            "The latest live run failed the M1 health gate; repair or demote failing sources.",
        )
    if clean_hours < window_hours:
        return (
            "pending",
            (
                "The current clean streak is healthy but has not covered the full "
                f"{_hours(window_hours)} evidence window yet."
            ),
        )
    if max_gap > max_gap_hours:
        return (
            "fail",
            (
                "The current clean streak covers the window, but the observed run gap "
                f"{_hours(max_gap)} exceeds the {_hours(max_gap_hours)} tolerance."
            ),
        )
    return "pass", "The current clean streak covers the evidence window and meets the health gate."


def _trailing_clean_streak(runs: list[RunRecord], min_health: float) -> list[RunRecord]:
    streak: list[RunRecord] = []
    for run in reversed(runs):
        if not run.is_clean(min_health):
            break
        streak.append(run)
    return list(reversed(streak))


def _gap_relevant_runs(clean_streak: list[RunRecord], window_start: datetime) -> list[RunRecord]:
    previous: RunRecord | None = None
    in_window: list[RunRecord] = []
    for run in clean_streak:
        if run.started_at < window_start:
            previous = run
        else:
            in_window.append(run)
    if previous is not None:
        return [previous, *in_window]
    return in_window


def _max_gap_hours(runs: list[RunRecord]) -> float:
    if len(runs) < 2:
        return 0.0
    gaps = [
        _hours_between(previous.started_at, current.started_at)
        for previous, current in zip(runs, runs[1:])
    ]
    return max(gaps)


def _source_failure_ranking(runs: list[RunRecord]) -> list[dict[str, Any]]:
    failures: dict[str, dict[str, Any]] = {}
    for run in runs:
        for row in run.source_results:
            if row.get("m1_action") != "include" or _source_row_is_healthy(row):
                continue
            source_id = str(row.get("source_id") or "unknown")
            bucket = failures.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "bad_runs": 0,
                    "last_status": None,
                    "last_item_count": None,
                    "last_error": None,
                    "last_run_id": None,
                },
            )
            bucket["bad_runs"] += 1
            bucket["last_status"] = str(row.get("status") or "unknown")
            bucket["last_item_count"] = _as_int(row.get("item_count")) or 0
            bucket["last_error"] = _trim(str(row.get("error") or ""), 120) or None
            bucket["last_run_id"] = run.run_id
    return sorted(failures.values(), key=lambda row: (-row["bad_runs"], row["source_id"]))


def _source_row_is_healthy(row: dict[str, Any]) -> bool:
    item_count = _as_int(row.get("item_count"))
    return row.get("status") == "success" and item_count is not None and item_count > 0


def _run_payload(run: RunRecord | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "status": run.status,
        "total_new_items": run.total_new_items,
        "include_total": run.include_total,
        "healthy_include": run.healthy_include,
        "health_ratio": run.health_ratio,
        "github_actions": run.github_actions,
        "path": _display_path(run.path),
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _as_source_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"manifest missing {field}")
    return value


def _require_time(value: Any, field: str) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"manifest missing {field}")
    return parsed


def _parse_cli_time(value: str) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid --now value: {value}")
    return parsed


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _hours_between(start: datetime | None, end: datetime) -> float:
    if start is None:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600)


def _iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def _optional_percent(value: float | None) -> str:
    return "-" if value is None else _percent(value)


def _hours(value: float) -> str:
    return f"{value:.2f}h"


def _count_rate(count: int, total: int) -> str:
    if total == 0:
        return "0/0 (-)"
    return f"{count}/{total} ({count / total * 100:.2f}%)"


def _code_or_dash(value: str | None) -> str:
    return "-" if value is None else f"`{value}`"


def _runtime_label(value: bool | None) -> str:
    if value is True:
        return "`true`"
    if value is False:
        return "`false`"
    return "`unknown`"


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
