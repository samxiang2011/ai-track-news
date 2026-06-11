from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit

from .config import Source, load_sources


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_ROOT = ROOT / "data" / "snapshots"
DEFAULT_OUTPUT_ROOT = ROOT / "data" / "derived" / "experimental"
DEFAULT_SOURCES_CONFIG = ROOT / "config" / "sources.yml"
DEFAULT_TOPIC_RULES = ROOT / "config" / "topic_rules.json"
DEFAULT_WINDOW_HOURS = 48.0
DEFAULT_LIMIT_CLUSTERS = 50
DEFAULT_HALF_LIFE_HOURS = 24.0
SCHEMA_VERSION = "m2-experimental-v0"

TIER_WEIGHTS = {1: 1.5, 2: 1.0, 3: 1.0, 4: 1.2}
UNKNOWN_TIER = 99
TITLE_SIMILARITY_THRESHOLD = 0.5
TITLE_OVERLAP_THRESHOLD = 0.6
MIN_SHARED_TITLE_TOKENS = 3
NEAR_DUPLICATE_THRESHOLD = 0.75

STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "and",
    "are",
    "artificial",
    "for",
    "from",
    "get",
    "gets",
    "into",
    "latest",
    "machine",
    "model",
    "models",
    "more",
    "new",
    "news",
    "not",
    "now",
    "open",
    "over",
    "said",
    "says",
    "that",
    "the",
    "their",
    "this",
    "through",
    "using",
    "with",
    "your",
}


@dataclass(frozen=True)
class SourceMeta:
    id: str
    tier: int
    topics: tuple[str, ...]


@dataclass(frozen=True)
class TopicRule:
    id: str
    source_hints: tuple[str, ...]
    keywords_zh: tuple[str, ...]
    keywords_en: tuple[str, ...]
    company_hints: tuple[str, ...]


@dataclass
class M2Item:
    id: str
    source_id: str
    url: str
    title: str
    published_at: str | None
    fetched_at: str
    lang: str
    topics: list[str]
    excerpt: str | None
    origin_url: str | None
    first_seen_at: datetime
    event_at: datetime
    tier: int
    domain: str
    title_tokens: set[str]

    def sidecar_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "lang": self.lang,
            "topics": self.topics,
        }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    now = _parse_cli_time(args.now) if args.now else datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-m2-exp"

    source_meta = load_source_meta(args.sources_config)
    topic_rules = load_topic_rules(args.topic_rules)
    snapshots = sorted(args.snapshot_root.glob("**/*-live.jsonl"))
    raw_items = load_snapshot_items(snapshots, source_meta, topic_rules)
    items = filter_items_for_window(raw_items, now, args.window_hours)
    clusters = build_clusters(items, now=now, half_life_hours=args.half_life_hours)
    limited_clusters = clusters[: args.limit_clusters]

    output = build_output(
        run_id=run_id,
        generated_at=now,
        snapshot_paths=snapshots,
        all_items=raw_items,
        window_items=items,
        clusters=limited_clusters,
        parameters={
            "window_hours": args.window_hours,
            "limit_clusters": args.limit_clusters,
            "half_life_hours": args.half_life_hours,
            "title_similarity_threshold": TITLE_SIMILARITY_THRESHOLD,
            "title_overlap_threshold": TITLE_OVERLAP_THRESHOLD,
            "min_shared_title_tokens": MIN_SHARED_TITLE_TOKENS,
            "tier_weights": TIER_WEIGHTS,
            "llm_calls": 0,
        },
    )

    output_dir = args.output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    clusters_path = output_dir / "clusters.json"
    review_path = output_dir / "cluster-review.md"
    clusters_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    review_path.write_text(format_review_markdown(output), encoding="utf-8")

    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": "success",
                "clusters": len(limited_clusters),
                "window_items": len(items),
                "output_paths": [
                    _relative(clusters_path),
                    _relative(review_path),
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local M2 experimental clustering.")
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--sources-config", type=Path, default=DEFAULT_SOURCES_CONFIG)
    parser.add_argument("--topic-rules", type=Path, default=DEFAULT_TOPIC_RULES)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--limit-clusters", type=int, default=DEFAULT_LIMIT_CLUSTERS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--now", help="Override current UTC time for deterministic output.")
    parser.add_argument("--half-life-hours", type=float, default=DEFAULT_HALF_LIFE_HOURS)
    return parser.parse_args(argv)


def load_source_meta(path: Path) -> dict[str, SourceMeta]:
    sources = load_sources(path)
    return {source.id: _source_to_meta(source) for source in sources}


def load_topic_rules(path: Path) -> list[TopicRule]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rules = []
    for raw_rule in data.get("topics", []):
        if not isinstance(raw_rule, dict):
            continue
        topic_id = raw_rule.get("id")
        if not isinstance(topic_id, str) or not topic_id:
            continue
        rules.append(
            TopicRule(
                id=topic_id,
                source_hints=tuple(_strings(raw_rule.get("source_hints"))),
                keywords_zh=tuple(_strings(raw_rule.get("keywords_zh"))),
                keywords_en=tuple(_strings(raw_rule.get("keywords_en"))),
                company_hints=tuple(_strings(raw_rule.get("company_hints"))),
            )
        )
    return rules


def load_snapshot_items(
    snapshot_paths: list[Path],
    source_meta: dict[str, SourceMeta],
    topic_rules: list[TopicRule],
) -> list[M2Item]:
    by_identity: dict[str, M2Item] = {}
    id_to_key: dict[str, str] = {}
    url_to_key: dict[str, str] = {}
    for path in snapshot_paths:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                raw = json.loads(line)
                item = item_from_raw(raw, source_meta, topic_rules)
                key = id_to_key.get(item.id) or url_to_key.get(item.url) or item.id
                id_to_key[item.id] = key
                url_to_key[item.url] = key
                existing = by_identity.get(key)
                if existing is None or item.first_seen_at < existing.first_seen_at:
                    by_identity[key] = item
    return sorted(by_identity.values(), key=lambda item: (item.event_at, item.id))


def item_from_raw(
    raw: dict[str, Any],
    source_meta: dict[str, SourceMeta],
    topic_rules: list[TopicRule],
) -> M2Item:
    item_id = _require_str(raw.get("id"), "id")
    source_id = _require_str(raw.get("source_id"), "source_id")
    url = _require_str(raw.get("url"), "url")
    title = _require_str(raw.get("title"), "title")
    fetched_at = _require_str(raw.get("fetched_at"), "fetched_at")
    fetched_time = _require_time(fetched_at, "fetched_at")
    published_at = raw.get("published_at") if isinstance(raw.get("published_at"), str) else None
    published_time = _parse_time(published_at)
    meta = source_meta.get(source_id, SourceMeta(source_id, UNKNOWN_TIER, ()))
    topics = merge_topics(
        _strings(raw.get("topics")),
        meta.topics,
        raw=raw,
        source_id=source_id,
        topic_rules=topic_rules,
    )
    return M2Item(
        id=item_id,
        source_id=source_id,
        url=url,
        title=title,
        published_at=published_at,
        fetched_at=fetched_at,
        lang=str(raw.get("lang") or "unknown"),
        topics=topics,
        excerpt=raw.get("excerpt") if isinstance(raw.get("excerpt"), str) else None,
        origin_url=raw.get("origin_url") if isinstance(raw.get("origin_url"), str) else None,
        first_seen_at=fetched_time,
        event_at=published_time or fetched_time,
        tier=meta.tier,
        domain=domain_from_url(url),
        title_tokens=title_tokens(title),
    )


def filter_items_for_window(
    items: list[M2Item], now: datetime, window_hours: float
) -> list[M2Item]:
    now = _ensure_utc(now)
    window_start = now - timedelta(hours=window_hours)
    return [item for item in items if window_start <= item.event_at <= now]


def build_clusters(
    items: list[M2Item],
    now: datetime,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> list[dict[str, object]]:
    grouped_items = _cluster_items(items)
    clusters = [
        build_cluster_payload(group, now=now, half_life_hours=half_life_hours)
        for group in grouped_items
    ]
    return sorted(
        clusters,
        key=lambda cluster: (
            -float(cluster["heat_score"]),
            str(cluster["last_seen"]),
            str(cluster["id"]),
        ),
    )


def build_cluster_payload(
    items: list[M2Item],
    now: datetime,
    half_life_hours: float,
) -> dict[str, object]:
    ordered = sorted(items, key=lambda item: item.id)
    representative = choose_representative(ordered)
    source_ids = tuple(sorted({item.source_id for item in ordered}))
    topic_ids = tuple(sorted({topic for item in ordered for topic in item.topics}))
    first_seen = min(item.event_at for item in ordered)
    last_seen = max(item.event_at for item in ordered)
    tier_mix = build_tier_mix(ordered)
    heat_score = score_heat(ordered, now=now, half_life_hours=half_life_hours)
    review_flags = build_review_flags(ordered)
    cluster_id = stable_cluster_id([item.id for item in ordered])

    return {
        "id": cluster_id,
        "title": representative.title,
        "item_ids": [item.id for item in ordered],
        "source_ids": list(source_ids),
        "topic_ids": list(topic_ids),
        "tier_mix": tier_mix,
        "source_count": len(source_ids),
        "first_seen": _iso(first_seen),
        "last_seen": _iso(last_seen),
        "heat_score": round(heat_score, 4),
        "summary": None,
        "representative_url": representative.url,
        "review_flags": review_flags,
        "experimental": True,
    }


def _cluster_items(items: list[M2Item]) -> list[list[M2Item]]:
    if not items:
        return []

    parents = {item.id: item.id for item in items}
    by_id = {item.id: item for item in items}
    ordered = sorted(items, key=lambda item: item.id)

    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if should_cluster_titles(left, right):
                _union(parents, left.id, right.id)

    groups: dict[str, list[M2Item]] = {}
    for item_id, item in by_id.items():
        groups.setdefault(_find(parents, item_id), []).append(item)

    return [sorted(group, key=lambda item: item.id) for group in groups.values()]


def should_cluster_titles(left: M2Item, right: M2Item) -> bool:
    if normalize_title(left.title) == normalize_title(right.title):
        return True
    score = title_similarity(left.title_tokens, right.title_tokens)
    shared = len(left.title_tokens & right.title_tokens)
    smaller = max(1, min(len(left.title_tokens), len(right.title_tokens)))
    overlap = shared / smaller
    return (
        shared >= MIN_SHARED_TITLE_TOKENS
        and score >= TITLE_SIMILARITY_THRESHOLD
        and overlap >= TITLE_OVERLAP_THRESHOLD
    )


def choose_representative(items: list[M2Item]) -> M2Item:
    return sorted(items, key=lambda item: (item.tier, -item.event_at.timestamp(), item.id))[0]


def build_tier_mix(items: list[M2Item]) -> dict[str, int]:
    source_tiers: dict[str, int] = {}
    for item in items:
        source_tiers[item.source_id] = min(source_tiers.get(item.source_id, item.tier), item.tier)
    mix = {"tier1": 0, "tier2": 0, "tier3": 0, "tier4": 0, "unknown": 0}
    for tier in source_tiers.values():
        key = f"tier{tier}" if tier in {1, 2, 3, 4} else "unknown"
        mix[key] += 1
    return mix


def score_heat(
    items: list[M2Item],
    now: datetime,
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS,
) -> float:
    latest = max(item.event_at for item in items)
    unique_source_tiers: dict[str, int] = {}
    for item in items:
        unique_source_tiers[item.source_id] = min(
            unique_source_tiers.get(item.source_id, item.tier), item.tier
        )
    source_weight = sum(TIER_WEIGHTS.get(tier, 1.0) for tier in unique_source_tiers.values())
    delta_hours = max(0.0, (_ensure_utc(now) - latest).total_seconds() / 3600)
    return source_weight * math.exp(-delta_hours / half_life_hours)


def build_review_flags(items: list[M2Item]) -> list[str]:
    flags = []
    if len({item.source_id for item in items}) == 1:
        flags.append("single_source")
    if len({item.domain for item in items if item.domain}) == 1:
        flags.append("same_domain")
    if has_near_duplicate_titles(items):
        flags.append("near_duplicate_titles")
    return flags


def has_near_duplicate_titles(items: list[M2Item]) -> bool:
    ordered = sorted(items, key=lambda item: item.id)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            if title_similarity(left.title_tokens, right.title_tokens) >= NEAR_DUPLICATE_THRESHOLD:
                return True
    return False


def merge_topics(
    item_topics: list[str],
    source_topics: tuple[str, ...],
    raw: dict[str, Any],
    source_id: str,
    topic_rules: list[TopicRule],
) -> list[str]:
    topics = set(item_topics)
    topics.update(source_topics)
    for rule in topic_rules:
        if topic_rule_matches(rule, raw=raw, source_id=source_id):
            topics.add(rule.id)
    return sorted(topic for topic in topics if topic)


def topic_rule_matches(rule: TopicRule, raw: dict[str, Any], source_id: str) -> bool:
    if source_id in rule.source_hints:
        return True
    text_parts = [
        str(raw.get("title") or ""),
        str(raw.get("excerpt") or ""),
        str(raw.get("url") or ""),
    ]
    text = "\n".join(text_parts).lower()
    compact = re.sub(r"[\s_-]+", "", text)

    for keyword in rule.keywords_en:
        normalized = keyword.lower()
        if normalized in text or re.sub(r"[\s_-]+", "", normalized) in compact:
            return True
    for keyword in rule.keywords_zh:
        if keyword.lower() in text or re.sub(r"\s+", "", keyword.lower()) in compact:
            return True
    return False


def title_tokens(title: str) -> set[str]:
    lowered = title.lower()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+-]*", lowered)
        if len(token) >= 2 and token not in STOPWORDS
    }
    for match in re.findall(r"[\u4e00-\u9fff]{2,}", lowered):
        tokens.update(match[index : index + 2] for index in range(len(match) - 1))
    return tokens


def title_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title.lower())


def stable_cluster_id(item_ids: list[str]) -> str:
    joined = "\n".join(sorted(item_ids))
    return "m2exp-" + sha256(joined.encode("utf-8")).hexdigest()[:16]


def build_output(
    run_id: str,
    generated_at: datetime,
    snapshot_paths: list[Path],
    all_items: list[M2Item],
    window_items: list[M2Item],
    clusters: list[dict[str, object]],
    parameters: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": _iso(generated_at),
        "input": {
            "snapshot_paths": [_relative(path) for path in snapshot_paths],
            "snapshot_count": len(snapshot_paths),
            "deduped_item_count": len(all_items),
            "window_item_count": len(window_items),
        },
        "parameters": parameters,
        "summary": {
            "cluster_count": len(clusters),
            "top_heat_score": clusters[0]["heat_score"] if clusters else None,
            "experimental": True,
            "llm_calls": 0,
        },
        "clusters": clusters,
        "items": [item.sidecar_dict() for item in sorted(window_items, key=lambda item: item.id)],
    }


def format_review_markdown(output: dict[str, object]) -> str:
    clusters = output["clusters"]
    assert isinstance(clusters, list)
    lines = [
        "# M2 Experimental Cluster Review",
        "",
        f"- Run id: `{output['run_id']}`",
        f"- Generated at: `{output['generated_at']}`",
        f"- Schema: `{output['schema_version']}`",
        "- Status: experimental, deterministic, no LLM calls",
        "",
        "## Top Clusters",
        "",
    ]
    if not clusters:
        lines.append("No clusters found for the selected window.")
        lines.append("")
        return "\n".join(lines)

    for index, cluster in enumerate(clusters, start=1):
        assert isinstance(cluster, dict)
        flags = cluster.get("review_flags") or []
        source_ids = cluster.get("source_ids") or []
        topic_ids = cluster.get("topic_ids") or []
        item_ids = cluster.get("item_ids") or []
        lines.extend(
            [
                f"### {index}. {cluster['title']}",
                "",
                "- [ ] Accept cluster",
                "- [ ] Wrong merge",
                "- [ ] Missing related item",
                f"- Heat: `{cluster['heat_score']}`",
                f"- Sources: `{', '.join(source_ids)}`",
                f"- Topics: `{', '.join(topic_ids) if topic_ids else '-'}`",
                f"- Flags: `{', '.join(flags) if flags else '-'}`",
                f"- Representative URL: {cluster['representative_url']}",
                f"- Window: `{cluster['first_seen']}` to `{cluster['last_seen']}`",
                "",
                "Items:",
            ]
        )
        for item_id in item_ids:
            item = _find_output_item(output, str(item_id))
            if item:
                lines.append(f"- `{item_id}` {item['source_id']}: {item['title']}")
            else:
                lines.append(f"- `{item_id}`")
        lines.append("")
    return "\n".join(lines)


def _find_output_item(output: dict[str, object], item_id: str) -> dict[str, object] | None:
    items = output.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    return None


def _union(parents: dict[str, str], left: str, right: str) -> None:
    left_root = _find(parents, left)
    right_root = _find(parents, right)
    if left_root == right_root:
        return
    if left_root < right_root:
        parents[right_root] = left_root
    else:
        parents[left_root] = right_root


def _find(parents: dict[str, str], item_id: str) -> str:
    parent = parents[item_id]
    if parent != item_id:
        parents[item_id] = _find(parents, parent)
    return parents[item_id]


def _source_to_meta(source: Source) -> SourceMeta:
    return SourceMeta(id=source.id, tier=source.tier, topics=tuple(source.topics))


def domain_from_url(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"item missing {field}")
    return value


def _require_time(value: str, field: str) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"item missing {field}")
    return parsed


def _parse_cli_time(value: str) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid --now value: {value}")
    return parsed


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_utc(parsed)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
