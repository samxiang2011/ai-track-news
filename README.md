# AI Track News Execution Repo

Runnable M1 pipeline for AI Track News:

```text
fetch -> normalize -> dedupe -> snapshot + manifest
```

The design source of truth lives in
`/Users/sam/lab/projects/ai_track_news/BRIEF.md`. This repo only contains the
execution layer.

## Quick Start

Run a local dry run without network access:

```bash
python3 -m pipeline.run_m1 --dry-run
```

Run a live M1 collection for include sources:

```bash
python3 -m pipeline.run_m1
```

Probe sources are excluded by default. Include them explicitly:

```bash
python3 -m pipeline.run_m1 --include-probes
```

Dry-run outputs go under `data/dry-runs/` and are ignored by git. Live snapshots
and manifests are intended to be committed after review or by the future
workflow.

## GitHub Actions

`.github/workflows/m1-hourly.yml` runs the M1 collector hourly and can also be
started manually. It commits `data/snapshots/` and `data/manifests/` only when
those outputs change.

## Outputs

- `data/snapshots/YYYY-MM/<run_id>.jsonl`
- `data/manifests/YYYY-MM/<run_id>.json`

Each item follows the M1 schema: id, source id, canonical URL, title,
published/fetched timestamps, language, limited excerpt, topics, and reserved
cluster/origin fields.

## Source Health

The M1 gate is at least 80% healthy include sources over the validation window.
Probe sources are exploratory and excluded from the gate denominator.
