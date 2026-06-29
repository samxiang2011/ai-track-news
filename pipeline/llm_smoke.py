"""LLM smoke test: prove the GLM runtime and probe cross-source clustering.

Two purposes, run in one pass:
  1. Runtime proof (needs LLM_API_KEY): auth, endpoint, model-id discovery,
     json_object mode, usage/quota reporting.
  2. Clustering-route signal: feed real recent items to GLM and count how many
     resulting clusters are multi-source, against the deterministic baseline of
     0/20. This informs whether M2 should route to LLM clustering.

Without a key the probe runs in MOCK mode: it exercises the full report pipeline
with synthetic clusters so the harness is verifiable offline. MOCK output is
never evidence of GLM behavior.

Outputs are gitignored under data/derived/experimental/llm-smoke/<run_id>/.
This does not touch the M1 workflow or formal M2; it is a probe.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .config import load_sources
from .llm import GLMClient, LLMError, LLMNotConfigured, pick_model


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_ROOT = ROOT / "data" / "snapshots"
DEFAULT_SOURCES_CONFIG = ROOT / "config" / "sources.yml"
DEFAULT_OUTPUT_ROOT = ROOT / "data" / "derived" / "experimental"
DEFAULT_WINDOW_HOURS = 48.0
DEFAULT_LIMIT_ITEMS = 70
DEFAULT_CLASSIFY_SAMPLE = 8
SCHEMA_VERSION = "llm-smoke-v0"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-llm-smoke"

    sources = load_sources(args.sources_config)
    source_map = {src.id: src.name for src in sources}
    items = load_recent_items(args.snapshot_root, now, args.window_hours, args.limit_items)
    if not items:
        print(
            json.dumps(
                {"status": "no_items", "window_hours": args.window_hours}, ensure_ascii=False
            )
        )
        return 1

    client = GLMClient()
    mode = "live" if client.available else "mock"
    if mode == "live":
        result = _run(client, items, source_map, args.classify_sample)
    else:
        result = _mock(items, source_map)
    result.update(
        {
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(now),
            "mode": mode,
            "base_url": client.base_url,
            "model": result.get("model") or client.model,
            "window_hours": args.window_hours,
            "item_count": len(items),
            "multi_source_clusters": _count_multi_source(result.get("clusters", []), items),
        }
    )
    out_dir = args.output_root / "llm-smoke" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "smoke.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "smoke.md").write_text(_markdown(result, items), encoding="utf-8")

    print(json.dumps(_summary(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run(
    client: GLMClient, items: list[dict[str, Any]], source_map: dict[str, str], classify_sample: int
) -> dict[str, Any]:
    discovery: dict[str, Any] = {}
    try:
        models = client.list_models()
    except LLMError as exc:
        discovery["list_models_error"] = str(exc)
        models = []

    discovery["available_model_ids"] = [str(m.get("id")) for m in models if m.get("id")]
    if not client.model:
        picked = pick_model(models)
        if picked:
            client.model = picked
    discovery["chosen_model"] = client.model

    classification: Any = None
    clusters: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        classification = _classify(client, items[:classify_sample])
    except LLMError as exc:
        errors.append(f"classify: {exc}")
    try:
        clusters = _cluster(client, items)
    except LLMError as exc:
        errors.append(f"cluster: {exc}")

    return {
        "discovery": discovery,
        "classification": classification,
        "clusters": clusters,
        "usage": client.usage.to_dict(),
        "errors": errors,
    }


def _classify(client: GLMClient, sample: list[dict[str, Any]]) -> Any:
    payload = [
        {"id": it["id"], "title": it["title"]}
        for it in sample
    ]
    system = (
        "You are a news topic classifier. Reply ONLY with a JSON object: "
        '{"results":[{"id":str,"topic":str}]}. topic must be one of: '
        "token-export, model-release, research, policy, funding, product, other."
    )
    user = "Classify each item:\n" + json.dumps(payload, ensure_ascii=False)
    parsed, _ = client.chat_json(system, user, max_tokens=1000, temperature=0.0)
    return parsed


def _cluster(client: GLMClient, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = [
        {
            "id": it["id"],
            "source_id": it["source_id"],
            "title": it["title"],
            "excerpt": it["excerpt"],
        }
        for it in items
    ]
    system = (
        "You cluster AI news items into EVENTS. An event = the same underlying "
        "happening covered across sources. Merge items from DIFFERENT sources only "
        "when they are truly about the same event; otherwise leave them as "
        "single-item clusters. Reply ONLY with JSON: "
        '{"clusters":[{"item_ids":[str],"title_zh":str,"summary_zh":str}]}. '
        "title_zh and summary_zh must be Chinese; summary_zh is 2-3 sentences."
    )
    user = "Cluster these items (source_id differs => independent source):\n" + json.dumps(
        payload, ensure_ascii=False
    )
    parsed, _ = client.chat_json(system, user, max_tokens=4000, temperature=0.1)
    raw = parsed.get("clusters") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict) and c.get("item_ids")]


def _mock(items: list[dict[str, Any]], source_map: dict[str, str]) -> dict[str, Any]:
    """Synthetic clusters to verify the report/counting pipeline offline."""
    by_source: dict[str, str] = {}
    for it in items:
        by_source.setdefault(it["source_id"], it["id"])
    ids = list(by_source.values())
    synthetic = [
        {
            "item_ids": ids[:3],
            "title_zh": "(mock) 示例多源事件标题",
            "summary_zh": "(mock) 这是占位摘要，仅用于验证报告管道，不代表 GLM 行为。",
        },
        {
            "item_ids": ids[3:4] if len(ids) > 3 else [],
            "title_zh": "(mock) 单源条目",
            "summary_zh": "(mock)",
        },
    ]
    return {
        "discovery": {"note": "mock mode; no key, no live call"},
        "classification": None,
        "clusters": [c for c in synthetic if c["item_ids"]],
        "usage": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0},
        "errors": [],
    }


def load_recent_items(
    root: Path, now: datetime, window_hours: float, limit: int
) -> list[dict[str, Any]]:
    """Load deduped recent items from committed snapshots. Public-safe fields only."""
    seen: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("**/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = item.get("id")
            if not item_id:
                continue
            fetched = _parse_time(item.get("fetched_at"))
            if fetched is None:
                continue
            age = (now - fetched).total_seconds() / 3600.0
            if age > window_hours or age < -1.0:
                continue
            existing = seen.get(item_id)
            prev = _parse_time(existing["fetched_at"]) if existing else None
            if existing is None or fetched > prev:  # type: ignore[operator]
                seen[item_id] = {
                    "id": str(item_id),
                    "source_id": str(item.get("source_id") or ""),
                    "title": str(item.get("title") or "")[:200],
                    "excerpt": str(item.get("excerpt") or "")[:200],
                    "fetched_at": item.get("fetched_at"),
                    "url": str(item.get("url") or ""),
                }
    ordered = sorted(seen.values(), key=lambda it: it["fetched_at"] or "", reverse=True)
    return ordered[:limit]


def _count_multi_source(clusters: list[dict[str, Any]], items: list[dict[str, Any]]) -> int:
    source_by_id = {it["id"]: it["source_id"] for it in items}
    count = 0
    for cluster in clusters:
        ids = [str(x) for x in cluster.get("item_ids", []) if x]
        sources = {source_by_id.get(i) for i in ids if source_by_id.get(i)}
        if len(sources) >= 2:
            count += 1
    return count


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    clusters = result.get("clusters", [])
    if result.get("mode") == "mock":
        status = "mock"
    elif result.get("errors"):
        status = "live_with_errors"
    else:
        status = "live"
    report_dir = Path("data/derived/experimental/llm-smoke") / result.get("run_id", "")
    return {
        "status": status,
        "mode": result.get("mode"),
        "model": result.get("model"),
        "base_url": result.get("base_url"),
        "item_count": result.get("item_count"),
        "cluster_count": len(clusters),
        "multi_source_clusters": result.get("multi_source_clusters"),
        "usage": result.get("usage"),
        "errors": result.get("errors"),
        "report": str(report_dir / "smoke.json"),
    }


def _markdown(result: dict[str, Any], items: list[dict[str, Any]]) -> str:
    source_by_id = {it["id"]: it["source_id"] for it in items}
    lines = [
        f"# LLM Smoke {result.get('run_id')}",
        "",
        f"- mode: **{result.get('mode')}**",
        f"- model: {result.get('model')}",
        f"- base_url: {result.get('base_url')}",
        f"- items fed: {result.get('item_count')}",
        f"- clusters: {len(result.get('clusters', []))}",
        f"- multi-source clusters: {result.get('multi_source_clusters')}",
        f"- usage: {result.get('usage')}",
        f"- errors: {result.get('errors')}",
        "",
        "## Clusters",
        "",
    ]
    for c in result.get("clusters", []):
        ids = [str(x) for x in c.get("item_ids", []) if x]
        srcs = sorted({source_by_id.get(i, "?") for i in ids})
        lines.append(f"### {c.get('title_zh')}")
        lines.append(f"- sources ({len(srcs)}): {', '.join(srcs)}")
        lines.append(f"- summary: {c.get('summary_zh')}")
        lines.append("")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GLM runtime + clustering smoke test.")
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--sources-config", type=Path, default=DEFAULT_SOURCES_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--limit-items", type=int, default=DEFAULT_LIMIT_ITEMS)
    parser.add_argument("--classify-sample", type=int, default=DEFAULT_CLASSIFY_SAMPLE)
    return parser.parse_args(argv)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
