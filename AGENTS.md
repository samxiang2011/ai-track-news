# AGENTS.md — ai_track_news Execution Repo

## Scope

This repository is the execution layer for the `~/lab/projects/ai_track_news`
design project. It contains runnable pipeline code, runtime config, snapshots,
manifests, generated site files, and GitHub Actions workflows.

The design source of truth remains in:

- `/Users/sam/lab/projects/ai_track_news/BRIEF.md`
- `/Users/sam/lab/projects/ai_track_news/wiki/`
- `/Users/sam/lab/projects/ai_track_news/ops/`

Do not migrate lab knowledge or full design documents here. Copy only concise
execution-facing summaries when needed.

## Safety

- Do not commit secrets, API keys, tokens, cookies, private keys, or local env
  files.
- Read LLM credentials from `LLM_API_KEY` only.
- Public data must not include raw HTML, full article bodies, or paywalled
  content.
- Excerpts are capped at 200 characters.

## Runtime Rules

- M1 is fetch, normalize, dedupe, snapshot, and manifest only.
- LLM-dependent topic fallback, clustering, ranking, and summarization start in
  later milestones.
- `config/sources.yml` is the running source-list truth once this repo exists.
- Only sources with `m1_action: include` count toward the M1 health gate.
  `probe` sources are exploratory and must not block M1.

## Editing

- Prefer small, direct Python changes.
- Do not introduce vector databases, queues, dynamic web backends, or heavy
  frontend frameworks.
- Keep generated dry-run outputs under `data/dry-runs/`, which is gitignored.
