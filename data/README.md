# Data Directory

Live M1 runs write append-only JSONL snapshots and JSON manifests here:

- `snapshots/YYYY-MM/<run_id>.jsonl`
- `manifests/YYYY-MM/<run_id>.json`

Dry runs write to `data/dry-runs/`, which is gitignored.

Derived stores such as SQLite are rebuildable and should stay out of git.
