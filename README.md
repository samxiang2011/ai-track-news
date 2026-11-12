# AI Track News Execution Repo

Runnable M1 pipeline for AI Track News:

```text
fetch -> normalize -> dedupe -> snapshot + manifest
```

This repo contains the execution layer only. Design decisions are maintained
upstream and are not mirrored here.

## Local environment

Local commands use this repository's ignored `.venv`. On this Mac:

```bash
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python --version
.venv/bin/python -m pip check
```

## Quick Start

Run a local dry run without network access:

```bash
.venv/bin/python -m pipeline.run_m1 --dry-run
```

Run a live M1 collection for include sources:

```bash
.venv/bin/python -m pipeline.run_m1
```

Report M1 health acceptance status from committed manifests:

```bash
.venv/bin/python -m pipeline.report_m1_health
```

Run the local-only M2 experimental clustering scaffold:

```bash
.venv/bin/python -m pipeline.run_m2_experimental --limit-clusters 20
```

The review output caps single-source clusters per source by default so one feed
does not dominate the top list. Pass `--max-clusters-per-source 0` to disable
that cap while debugging.

Probe sources are excluded by default. Include them explicitly:

```bash
.venv/bin/python -m pipeline.run_m1 --include-probes
```

Render the static M3a Portal from the latest experimental clusters:

```bash
.venv/bin/python -m pipeline.render_site
```

Dry-run outputs go under `data/dry-runs/` and are ignored by git. Live snapshots
and manifests are intended to be committed after review or by the future
workflow.

## GitHub Actions

`.github/workflows/m1-hourly.yml` runs the M1 collector hourly and can also be
started manually. It writes an M1 health acceptance summary to the Actions run
and commits `data/snapshots/` and `data/manifests/` when those outputs change.
It no longer clusters, renders, or deploys the public Portal.

## Outputs

- `data/snapshots/YYYY-MM/<run_id>.jsonl`
- `data/manifests/YYYY-MM/<run_id>.json`
- `data/derived/experimental/<run_id>/clusters.json` for local M2 experiments
- `data/derived/experimental/<run_id>/cluster-review.md` for local M2 review
- `site/index.html` and `site/sources/index.html` for the M3a Portal

Each item follows the M1 schema: id, source id, canonical URL, title,
published/fetched timestamps, language, limited excerpt, topics, and reserved
cluster/origin fields.

`data/derived/` is ignored by git. M2 experimental outputs are disposable and
do not affect the M1 acceptance gate. `site/` is committed because it is the
public GitHub Pages artifact.

## Source Health

The M1 gate is at least 80% healthy include sources over the validation window.
Probe sources are exploratory and excluded from the gate denominator.

The health report uses the latest clean streak across live manifests. It stays
`pending` until the streak covers the 72-hour evidence window, returns `fail` if
the latest live run misses the health gate, and returns `pass` only after the
window is covered without excessive run gaps. The default run-gap tolerance is 8
hours to allow for GitHub Actions' best-effort cron scheduling while still
flagging material collection stalls.
