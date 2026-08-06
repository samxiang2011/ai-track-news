# AGENTS.md — ai_track_news Execution Repo

<!-- container-rules: generated block, do not edit by hand -->
<!-- container-rules:begin -->
## Repository Rules

### Repository Contents

- Do not add or migrate a second layer of design documents or durable knowledge
  into this repository. Design decisions are made outside it.

### Authorization

- A request to review, diagnose, or read does not authorize changing anything.
- Being asked to change code does not authorize committing, pushing, deploying,
  publishing, or releasing. Each of those needs its own explicit instruction.
- If you cannot see the accepted scope or design contract for this repository, do
  not infer it from the code. Ask. Do not extend or change accepted behaviour on
  your own judgement.

### Safety

- Do not read, print, copy, or commit secrets, API keys, tokens, cookies,
  private keys, credentials, shell history, or private credential stores.
- Do not commit `.env`, `.env.*`, local credential files, raw paid data, or
  heavyweight generated artifacts unless this repository's own rules explicitly
  allow a specific non-sensitive artifact. A committed `.env.example` or
  `.env.template` is fine when it contains placeholders only.
- Do not run destructive shell commands, force-push, or broad permission changes
  without explicit approval.
- Confirm the exact target, and that you were asked, before deleting,
  overwriting, migrating, or any other irreversible transform.

### Working Alongside Other Work

- When several agents share one working tree, a file has at most one active
  writer at a time; everyone else reads only.
- Re-read a file immediately before writing it. If it changed since you last read
  it, stop and re-read instead of overwriting.
- Check uncommitted changes before you start. Do not touch, commit, or discard
  work you did not begin, without an explicit instruction to do so.

### Verification and Handoff

- Claim only the verification you actually ran, and name what you did not verify.
- Close with an explicit status: what is finished and verifiable, what is not
  covered, and what would unblock the rest. Finished means the assigned scope is
  done, not that anyone accepted it or that it worked in reality.
- If the work changes accepted design or scope, reveals a reusable method or
  source-quality lesson, or changes handoff state, flag it to the maintainer
  instead of treating it as routine implementation.

### Commits

- Use Conventional Commits with a scope: `type(scope): summary`. Imperative mood,
  lower case, no trailing period. Existing history is mixed; follow this form, not
  the nearest neighbour.
- `data` and `cron` are reserved for automated commits; do not use them by hand.
- Keep one intent per commit. Do not bundle unrelated changes.

### Python Environments

- Before creating or rebuilding `.venv`, or installing Python packages, search
  upward from the working directory for an existing `.venv/bin/python`.
- Stop the search at this repository's git root; do not silently cross into
  anything outside this repository.
- This repository's own `.venv` owns its runtime and test dependencies.
- Validate a candidate environment by running that interpreter itself:
  `<candidate>/bin/python --version` and `<candidate>/bin/python -m pip check`.
  Never validate with a bare `python` or `python3`, which resolves elsewhere.
- Use an explicit interpreter path such as `.venv/bin/python`; never install
  project packages into the system Python.
<!-- container-rules:end -->

## Scope

This repository is the execution layer of a project whose design is maintained
outside it. It contains runnable pipeline code, runtime config, snapshots,
manifests, generated site files, and GitHub Actions workflows.

Design contracts, project wiki, and operational notes live in that upstream
workspace and are not mirrored here. Ask the maintainer if you need design
context; do not infer it from the code.

## Data and Credentials

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

- Do not introduce vector databases, queues, dynamic web backends, or heavy
  frontend frameworks.
- Keep generated dry-run outputs under `data/dry-runs/`, which is gitignored.
- Keep changes within the accepted milestone. Do not build later machinery or
  formalize experiments before that scope is explicitly extended.
- Choose the narrowest relevant evaluator: targeted `pytest`, dry run, live run,
  or `python3 -m pipeline.report_m1_health`.
