from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from .normalize import Item


def write_snapshot(root: Path, run_id: str, items: list[Item], dry_run: bool) -> Path:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    base = root / "data"
    if dry_run:
        base = base / "dry-runs"
    path = base / "snapshots" / month / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return path


def load_seen_item_ids(root: Path) -> set[str]:
    """Return the ids of every item already recorded in committed live snapshots.

    The set is rebuilt from ``data/snapshots/`` on each run, so it depends only on
    files tracked in the repository, never on runner-local state. Unreadable lines
    are skipped: a damaged old snapshot must not stop collection.
    """
    seen: set[str] = set()
    snapshot_root = root / "data" / "snapshots"
    if not snapshot_root.is_dir():
        return seen
    for path in sorted(snapshot_root.glob("*/*.jsonl")):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                item_id = record.get("id") if isinstance(record, dict) else None
                if isinstance(item_id, str) and item_id:
                    seen.add(item_id)
    return seen


def select_new_items(items: list[Item], seen_ids: set[str]) -> list[Item]:
    return [item for item in items if item.id not in seen_ids]


def write_manifest(root: Path, run_id: str, manifest: dict[str, object], dry_run: bool) -> Path:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    base = root / "data"
    if dry_run:
        base = base / "dry-runs"
    path = base / "manifests" / month / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def git_commit_or_none(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    commit = result.stdout.strip()
    return None if commit == "HEAD" else commit
