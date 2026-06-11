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
