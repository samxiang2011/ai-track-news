# Design Contract Summary

This summarises the execution constraints this repo is built against. The full
design contract is maintained upstream and is not mirrored here.

Execution constraints for M1:

- Python single-repo pipeline.
- Fetch only public RSS/API/page metadata; never store raw HTML.
- Store title, canonical URL, timestamps, source id, language, topics, and
  limited excerpts only.
- Public excerpts must be capped at 200 characters.
- Write append-only JSONL snapshots and per-run manifests.
- Isolate per-source failures. One failed source must not fail the whole run.
- Count only `m1_action: include` sources toward source-health gating.
- Do not start M2 clustering while include-source health is below 80%.
- No LLM is required for M1.

Runtime truth sources:

- Strategy and milestone truth: maintained upstream; ask the maintainer.
- Running source list: `config/sources.yml`.
- Schema needs and implementation discoveries: `docs/schema-needs.md`.
