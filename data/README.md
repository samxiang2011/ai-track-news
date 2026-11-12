# Data Directory

Live M1 runs write append-only JSONL snapshots and JSON manifests here:

- `snapshots/YYYY-MM/<run_id>.jsonl`
- `manifests/YYYY-MM/<run_id>.json`

Since 2026-11-12 each live snapshot holds only items whose `id` appears in no
earlier committed snapshot (manifest `snapshot_mode: new_items_only`); a run
with nothing new writes an empty file. Older snapshots hold the full deduped
fetch window of their run and are left as they were. Manifest `total_new_items`
still counts the deduped fetch window; `snapshot_item_count` counts the lines
written.

Dry runs write to `data/dry-runs/`, which is gitignored.

Derived stores such as SQLite are rebuildable and should stay out of git.
