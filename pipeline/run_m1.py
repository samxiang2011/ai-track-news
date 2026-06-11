from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from .config import Source, load_sources
from .fetch import fetch_source
from .normalize import Item, dedupe_items, normalize_entry
from .parse import parse_feed
from .storage import git_commit_or_none, write_manifest, write_snapshot


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "sources.yml"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started = datetime.now(timezone.utc)
    run_id = _run_id(started, args.dry_run)

    sources = _select_sources(load_sources(args.config), args.include_probes, args.limit_sources)
    source_results: list[dict[str, object]] = []
    items: list[Item] = []

    for source in sources:
        fetched = fetch_source(source, timeout=args.timeout, dry_run=args.dry_run)
        item_count = 0
        if fetched.status == "success" and fetched.content:
            try:
                raw_entries = parse_feed(source, fetched.content)
                normalized = [
                    normalize_entry(entry, fetched.fetched_at or datetime.now(timezone.utc))
                    for entry in raw_entries[: args.max_items_per_source]
                ]
                item_count = len(normalized)
                items.extend(normalized)
            except Exception as exc:  # noqa: BLE001 - bad feed must not kill the run.
                fetched = fetched.__class__(
                    source=fetched.source,
                    status="failed",
                    fetched_at=fetched.fetched_at,
                    content=None,
                    content_type=fetched.content_type,
                    error=f"parse error: {type(exc).__name__}: {exc}",
                )

        source_results.append(
            {
                "source_id": source.id,
                "status": fetched.status,
                "item_count": item_count,
                "error": fetched.error,
                "fetched_at": _iso(fetched.fetched_at) if fetched.fetched_at else None,
                "access_method": source.access_method,
                "m1_action": source.m1_action,
            }
        )

    deduped_items = dedupe_items(items)
    snapshot_path = write_snapshot(ROOT, run_id, deduped_items, dry_run=args.dry_run)

    finished = datetime.now(timezone.utc)
    manifest = _build_manifest(
        run_id=run_id,
        started=started,
        finished=finished,
        source_results=source_results,
        total_new_items=len(deduped_items),
        output_paths=[_relative(snapshot_path)],
    )
    manifest["git_commit"] = git_commit_or_none(ROOT)
    manifest_path = write_manifest(ROOT, run_id, manifest, dry_run=args.dry_run)
    manifest["output_paths"].append(_relative(manifest_path))
    write_manifest(ROOT, run_id, manifest, dry_run=args.dry_run)

    print(json.dumps(_summary(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if manifest["status"] in {"success", "partial"} else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run M1 fetch-normalize-dedupe pipeline.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use synthetic feeds and dry-run output paths.",
    )
    parser.add_argument("--include-probes", action="store_true", help="Also run probe sources.")
    parser.add_argument("--limit-sources", type=int, default=None)
    parser.add_argument("--max-items-per-source", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def _select_sources(
    sources: list[Source], include_probes: bool, limit: int | None
) -> list[Source]:
    selected = [
        source
        for source in sources
        if source.m1_action == "include" or (include_probes and source.m1_action == "probe")
    ]
    if limit is not None:
        return selected[:limit]
    return selected


def _build_manifest(
    run_id: str,
    started: datetime,
    finished: datetime,
    source_results: list[dict[str, object]],
    total_new_items: int,
    output_paths: list[str],
) -> dict[str, object]:
    include_results = [row for row in source_results if row["m1_action"] == "include"]
    healthy_include = sum(
        1
        for row in include_results
        if (
            row["status"] == "success"
            and isinstance(row["item_count"], int)
            and row["item_count"] > 0
        )
    )
    include_total = len(include_results)
    health_ratio = healthy_include / include_total if include_total else 0.0

    failed_count = sum(1 for row in source_results if row["status"] == "failed")
    success_count = sum(1 for row in source_results if row["status"] == "success")
    if success_count == 0 and source_results:
        status = "failed"
    elif failed_count:
        status = "partial"
    else:
        status = "success"

    return {
        "run_id": run_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "status": status,
        "source_results": source_results,
        "total_new_items": total_new_items,
        "total_clusters": None,
        "llm_usage": {
            "calls": 0,
            "prompt_tokens": None,
            "completion_tokens": None,
            "estimated_cost": None,
        },
        "source_health": {
            "include_total": include_total,
            "healthy_include": healthy_include,
            "health_ratio": round(health_ratio, 4),
            "gate_threshold": 0.8,
            "gate_passed": health_ratio >= 0.8,
        },
        "output_paths": output_paths,
        "git_commit": None,
        "runtime": _runtime_metadata(),
    }


def _summary(manifest: dict[str, object]) -> dict[str, object]:
    return {
        "run_id": manifest["run_id"],
        "status": manifest["status"],
        "total_new_items": manifest["total_new_items"],
        "source_health": manifest["source_health"],
        "output_paths": manifest["output_paths"],
    }


def _run_id(started: datetime, dry_run: bool) -> str:
    suffix = "dry" if dry_run else "live"
    return f"{started.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _runtime_metadata() -> dict[str, object]:
    github_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    metadata: dict[str, object] = {"github_actions": github_actions}
    if not github_actions:
        return metadata

    optional_fields = {
        "github_run_id": "GITHUB_RUN_ID",
        "github_run_attempt": "GITHUB_RUN_ATTEMPT",
        "github_workflow": "GITHUB_WORKFLOW",
        "github_job": "GITHUB_JOB",
        "github_repository": "GITHUB_REPOSITORY",
        "github_ref_name": "GITHUB_REF_NAME",
        "github_sha": "GITHUB_SHA",
    }
    for field_name, env_name in optional_fields.items():
        value = os.environ.get(env_name)
        if value:
            metadata[field_name] = value
    return metadata


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
