# Design Contract Summary

The full design contract lives in
`/Users/sam/lab/projects/ai_track_news/BRIEF.md`.

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

- Strategy and milestone truth: lab `BRIEF.md`.
- Running source list: `config/sources.yml`.
- Schema needs and implementation discoveries: `docs/schema-needs.md`.
