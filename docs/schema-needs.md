# Schema Needs Log

This log tracks M1 implementation pressure on the schema. It should be updated
at each milestone before human review.

## 2026-06-11 M1 Skeleton

- Added `source_health` to the run manifest output so M1 can report include
  source health directly. This is an execution extension over the minimal BRIEF
  manifest and should be reviewed at M1.
- Exact URL dedupe currently keeps one item globally. If the same canonical URL
  appears from several independent sources, M2 may need either mention records,
  `duplicate_source_ids`, or a cluster-level source aggregation step so
  source-count heat is not undercounted.
- `public_page` sources are configured but skipped in M1 unless a compliant
  per-source parser is added. This avoids pretending a landing page title is a
  news item.
