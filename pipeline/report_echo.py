"""独立性 / echo 度量:量化 top 聚类里"转载主导"的占比(R0 测量基线)。

只读分析层:读渲染好的 ``site/data/portal.json``,不改抓取/聚类/渲染主链路,
不写回数据或配置。

产品脊:热度 = 独立信源共现。转载/通稿会令 ``source_count`` 虚高
(多源共现 ≠ 独立证实)。本模块把预留的 echo 观察项从 v1 提前到 v0.x,
用每个 cluster 已有的 ``tier_mix`` 量化伪共现:

- ``tier_mix`` 由 ``build_tier_mix``(``run_m2_experimental.py``)按 ``sources.yml``
  的 tier 分桶:tier1=厂商一手官方、tier2=社区/研究、tier3=AI 媒体(常转载)、
  tier4=专项分析。
- 独立锚 = tier1/tier2/tier4 中任一 > 0(tier3 媒体不计独立锚,因其常转载公告)。
- ``repost_dominated`` = 多源 cluster(source_count ≥ 2)且无任何独立锚
  → 纯媒体多源,最虚高的伪共现形态。
- 附列标题两两相似度(内容层旁证,不进主判定):同一公告被多源转载时标题高度
  相似,可交叉验证 tier 判定。用标准库 ``difflib`` 独立实现,不耦合聚类模块。

CLI:``python -m pipeline.report_echo [--portal PATH] [--top N] [--format markdown|json] [--github-summary]``
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORTAL = ROOT / "site" / "data" / "portal.json"
DEFAULT_TOP_N = 10
_TIER_KEYS = ("tier1", "tier2", "tier3", "tier4", "unknown")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    portal = load_portal(args.portal)
    report = build_echo_report(portal, top_n=args.top)
    markdown = format_markdown(report)
    if args.github_summary:
        report["github_summary_written"] = write_github_summary(markdown)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(markdown)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report echo / source-independence metric over top clusters.")
    parser.add_argument("--portal", type=Path, default=DEFAULT_PORTAL)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_N, help="Number of top clusters to score (by rendered rank).")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--github-summary", action="store_true")
    return parser.parse_args(argv)


def load_portal(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tier_mix(cluster: dict[str, Any]) -> dict[str, int]:
    """Return a cleaned tier_mix dict; missing or non-int entries collapse to {}."""
    raw = cluster.get("tier_mix")
    if not isinstance(raw, dict):
        return {}
    mix: dict[str, int] = {}
    for key in _TIER_KEYS:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            mix[key] = value
    return mix


def _has_independent_anchor(mix: dict[str, int]) -> bool:
    return mix.get("tier1", 0) > 0 or mix.get("tier2", 0) > 0 or mix.get("tier4", 0) > 0


def _independent_classes(mix: dict[str, int]) -> int:
    return (
        (1 if mix.get("tier1", 0) > 0 else 0)
        + (1 if mix.get("tier2", 0) > 0 else 0)
        + (1 if mix.get("tier4", 0) > 0 else 0)
    )


def _max_title_similarity(titles: list[Any]) -> float | None:
    """Max pairwise similarity among item titles (content-layer echo corroboration)."""
    cleaned = [t for t in titles if isinstance(t, str) and t.strip()]
    if len(cleaned) < 2:
        return None
    best = 0.0
    for index, left in enumerate(cleaned):
        for right in cleaned[index + 1:]:
            best = max(best, SequenceMatcher(None, left.lower(), right.lower()).ratio())
    return round(best, 3)


def classify_cluster(cluster: dict[str, Any]) -> dict[str, Any]:
    source_count = cluster.get("source_count")
    if not isinstance(source_count, int) or isinstance(source_count, bool):
        source_count = len([s for s in (cluster.get("source_ids") or []) if isinstance(s, str)])
    mix = _tier_mix(cluster)
    multi_source = source_count >= 2
    # tier_mix missing or all-zero → cannot judge independence; exclude from denominator.
    scorable = multi_source and sum(mix.values()) > 0
    repost = scorable and not _has_independent_anchor(mix)
    return {
        "id": cluster.get("id"),
        "title": cluster.get("title") or "",
        "rank": cluster.get("rank"),
        "source_count": source_count,
        "source_names": [str(name) for name in (cluster.get("source_names") or [])],
        "tier_mix": mix,
        "multi_source": multi_source,
        "scorable": scorable,
        "repost_dominated": repost,
        "independent_classes": _independent_classes(mix) if scorable else None,
        "title_similarity": _max_title_similarity(cluster.get("item_titles") or []),
    }


def build_echo_report(portal: dict[str, Any], top_n: int) -> dict[str, Any]:
    clusters = portal.get("clusters") or []
    if not isinstance(clusters, list):
        clusters = []
    selected = clusters[: max(0, top_n)]
    classified = [classify_cluster(cluster) for cluster in selected]
    scorable = [row for row in classified if row["scorable"]]
    repost = [row for row in scorable if row["repost_dominated"]]
    independent_values = [
        row["independent_classes"] for row in scorable if row["independent_classes"] is not None
    ]

    return {
        "generated_at": portal.get("generated_at"),
        "criteria": {
            "top_n": top_n,
            "selected": len(selected),
            "scorable": len(scorable),
            "single_source_in_top": sum(1 for row in classified if not row["multi_source"]),
        },
        "repost_dominated_ratio": _ratio(len(repost), len(scorable)),
        "mean_independent_classes": _average(independent_values),
        "repost_dominated_clusters": [_summary(row) for row in repost],
        "top_clusters": [_summary(row) for row in classified],
    }


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "rank": row["rank"],
        "source_count": row["source_count"],
        "source_names": row["source_names"],
        "tier_mix": row["tier_mix"],
        "repost_dominated": row["repost_dominated"],
        "independent_classes": row["independent_classes"],
        "title_similarity": row["title_similarity"],
    }


def format_markdown(report: dict[str, Any]) -> str:
    criteria = report["criteria"]
    ratio = report["repost_dominated_ratio"]
    mean_classes = report["mean_independent_classes"]
    repost = report["repost_dominated_clusters"]

    lines = [
        "# Echo / Independence Report",
        "",
        "Top clusters scored for repost-dominated co-occurrence (no independent anchor).",
        "Read-only measurement; does not modify sources or pipeline.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Top clusters scored | {criteria['scorable']}/{criteria['selected']} scorable "
        f"({criteria['single_source_in_top']} single-source) |",
        f"| Repost-dominated ratio | {_optional_percent(ratio)} |",
        f"| Mean independent classes | {_optional_float(mean_classes)} |",
        f"| Repost-dominated clusters | {len(repost)} |",
        "",
    ]

    lines.extend(["## Repost-dominated Clusters", ""])
    if not repost:
        lines.append("No repost-dominated clusters in the scored top set.")
    else:
        lines.extend(
            [
                "| Rank | Title | Sources | Source names | Tier mix | Title sim |",
                "| ---: | --- | ---: | --- | --- | ---: |",
            ]
        )
        for row in repost:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _code_or_dash(row["rank"]),
                        _escape_table(_trim(row["title"], 60)),
                        str(row["source_count"]),
                        _escape_table(", ".join(row["source_names"]) or "-"),
                        _format_tier_mix(row["tier_mix"]),
                        _optional_float(row["title_similarity"]),
                    ]
                )
                + " |"
            )

    top = report["top_clusters"]
    lines.extend(["", "## Top-cluster Detail", ""])
    if not top:
        lines.append("No clusters available.")
    else:
        lines.extend(
            [
                "| Rank | Title | Src | Indep cls | Repost? | Title sim |",
                "| ---: | --- | ---: | ---: | :---: | ---: |",
            ]
        )
        for row in top:
            indep = row["independent_classes"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        _code_or_dash(row["rank"]),
                        _escape_table(_trim(row["title"], 50)),
                        str(row["source_count"]),
                        "-" if indep is None else str(indep),
                        "yes" if row["repost_dominated"] else "no",
                        _optional_float(row["title_similarity"]),
                    ]
                )
                + " |"
            )

    return "\n".join(lines)


def write_github_summary(markdown: str) -> bool:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return False
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write(markdown)
        handle.write("\n")
    return True


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _average(values: list[int]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _format_tier_mix(mix: dict[str, int]) -> str:
    if not mix:
        return "-"
    return " ".join(f"{key}={mix[key]}" for key in _TIER_KEYS if mix.get(key))


def _optional_percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def _optional_float(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _code_or_dash(value: object) -> str:
    return "-" if value is None else f"`{value}`"


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
