# Schema Needs Log

This log tracks M1 implementation pressure on the schema. It should be updated
at each milestone before human review.

## 2026-06-11 M1 Skeleton

- Added `source_health` to the run manifest output so M1 can report include
  source health directly. This is an execution extension over the minimal BRIEF
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
