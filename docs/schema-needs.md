# Schema Needs Log

This log tracks M1 implementation pressure on the schema. It should be updated
at each milestone before human review.

## 2026-06-11 M1 Skeleton

- Added `source_health` to the run manifest output so M1 can report include
  source health directly. This is an execution extension over the minimal accepted
  manifest and should be reviewed at M1.
- Tightened source health after the first live validation: an include source now
  counts as healthy only when it fetches successfully and yields at least one
  normalized item. A feed that returns 200/OK but parses to zero items is not
  useful for M1 validation.
- Added non-sensitive `runtime.github_actions` metadata to new manifests so the
  M1 acceptance report can distinguish future GitHub-hosted runs from local
  validation runs. Older manifests do not contain this field and report as
  unknown runner metadata.
- Exact URL dedupe currently keeps one item globally. If the same canonical URL
  appears from several independent sources, M2 may need either mention records,
  `duplicate_source_ids`, or a cluster-level source aggregation step so
  source-count heat is not undercounted.
- `public_page` sources are configured but skipped in M1 unless a compliant
  per-source parser is added. This avoids pretending a landing page title is a
  news item.

## 2026-06-11 M2 Experimental Scaffold

- The experimental M2 scaffold is deterministic and local-only: no LLM calls,
  no GitHub Actions wiring, and outputs are written under gitignored
  `data/derived/experimental/`.
- Cluster titles currently use the highest-tier, most recent representative
  item. This is a placeholder until LLM-generated Chinese titles and summaries
  are added after the M1 gate.
- Clustering uses exact item consolidation plus title-token similarity. It does
  not yet solve origin-story grouping, syndicated echoes, or shared-source
  duplication. Review flags expose `single_source`, `same_domain`, and
  `near_duplicate_titles` so top clusters can be manually audited.
- `token-export` tagging uses execution-facing keyword/source-hint rules copied
  from the accepted lab seed taxonomy. There is no LLM fallback yet, so recall
  and precision should be reviewed before promotion to formal M2.

## 2026-06-11 M2 Scaffold Tuning

- Added a local review cap for single-source clusters so HN/community feeds do
  not dominate the top review list purely by recency. This is a review-surface
  control, not a final ranking policy.
- Added limited event-key clustering for obvious launch/event families such as
  Claude/Fable/Mythos. This improves recall for current data but should be
  replaced by a broader entity/event layer before formal M2.
- Tightened `token-export` rules so weak benchmark/evaluation words do not
  trigger the topic without model/company/channel context.
- `same_domain` is now emitted only for multi-item clusters, reducing noise in
  single-item review output.

## 2026-06-30 R0 Echo Metric + Auto-demote (read-only measurement layer)

- Added `pipeline/report_echo.py`: quantifies repost-dominated co-occurrence over
  top-N rendered clusters using each cluster's existing `tier_mix`. A multi-source
  cluster with no tier1/tier2/tier4 anchor (pure tier3 media) is
  `repost_dominated` — the most inflated echo form. Title pairwise similarity is
  an attached corroboration column, not a primary signal. This realises the planned
  echo observation item ahead of v1 as the R0 measurement baseline.
- Extended `pipeline/report_m1_health.py` with `demotion_candidates`: per
  include-source zero-item rate (`status=success, item_count=0`) over the window.
  Sources above `--demote-threshold` (default 0.5) with enough samples
  (`--demote-min-samples`, default 5) are flagged as candidates. Suggestion only —
  it never edits `sources.yml` (source changes stay a human git-review gate).
- Both are read-only analysis; neither touches the fetch/cluster/render main path.
- R0 baseline (2026-06-30): top10 `repost_dominated_ratio` 0.00%, mean independent
  classes 1.000 (every multi-source cluster has ≥1 independent anchor, but only
  one class on average — independence is thin, the lever for R1 source additions).
  Demotion: `arxiv-cs-cl` 64% zero rate (16/25, candidate); `arxiv-cs-ai` 48%
  (12/25, just under threshold) — establishes the R1 fix priority (cs-cl first).
