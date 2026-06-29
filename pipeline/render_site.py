from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import shutil
from typing import Any

from .config import Source, load_sources
from .report_m1_health import (
    DEFAULT_MAX_GAP_HOURS,
    DEFAULT_MIN_HEALTH,
    DEFAULT_WINDOW_HOURS,
    RunRecord,
    build_report,
    load_runs,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLUSTERS_ROOT = ROOT / "data" / "derived" / "experimental"
DEFAULT_MANIFEST_ROOT = ROOT / "data" / "manifests"
DEFAULT_SOURCES_CONFIG = ROOT / "config" / "sources.yml"
DEFAULT_OUTPUT_DIR = ROOT / "site"
DEFAULT_LIMIT_CLUSTERS = 20
DEFAULT_LIMIT_ITEMS = 80
DISCLAIMER = "AI 自动聚合排序，未经人工审核，仅为注意力分配建议"
SCHEMA_VERSION = "m3a-portal-v0"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    now = _parse_cli_time(args.now) if args.now else datetime.now(timezone.utc)
    clusters_json = args.clusters_json or find_latest_clusters_json(args.clusters_root)
    if clusters_json is None:
        raise SystemExit("No clusters.json found. Run pipeline.run_m2_experimental first.")

    portal = build_portal(
        clusters_path=clusters_json,
        manifest_root=args.manifest_root,
        sources_config=args.sources_config,
        now=now,
        limit_clusters=args.limit_clusters,
        limit_items=args.limit_items,
    )
    write_site(portal, args.output_dir)
    print(
        json.dumps(
            {
                "status": "success",
                "output_dir": _relative(args.output_dir),
                "clusters_json": _relative(clusters_json),
                "files": [
                    _relative(args.output_dir / "index.html"),
                    _relative(args.output_dir / "sources" / "index.html"),
                    _relative(args.output_dir / "assets" / "styles.css"),
                    _relative(args.output_dir / "data" / "portal.json"),
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the static M3a Portal site.")
    parser.add_argument("--clusters-json", type=Path)
    parser.add_argument("--clusters-root", type=Path, default=DEFAULT_CLUSTERS_ROOT)
    parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    parser.add_argument("--sources-config", type=Path, default=DEFAULT_SOURCES_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit-clusters", type=int, default=DEFAULT_LIMIT_CLUSTERS)
    parser.add_argument("--limit-items", type=int, default=DEFAULT_LIMIT_ITEMS)
    parser.add_argument("--now", help="Override current UTC time for deterministic output.")
    return parser.parse_args(argv)


def build_portal(
    clusters_path: Path,
    manifest_root: Path,
    sources_config: Path,
    now: datetime,
    limit_clusters: int = DEFAULT_LIMIT_CLUSTERS,
    limit_items: int = DEFAULT_LIMIT_ITEMS,
) -> dict[str, Any]:
    now = _ensure_utc(now)
    clusters_data = json.loads(clusters_path.read_text(encoding="utf-8"))
    sources = load_sources(sources_config)
    source_map = {source.id: source for source in sources}
    runs = load_runs(manifest_root)
    latest_run = runs[-1] if runs else None
    health_report = build_report(
        runs=runs,
        now=now,
        window_hours=DEFAULT_WINDOW_HOURS,
        min_health=DEFAULT_MIN_HEALTH,
        max_gap_hours=DEFAULT_MAX_GAP_HOURS,
    )

    clusters = normalize_clusters(
        clusters_data,
        source_map=source_map,
        now=now,
        limit=limit_clusters,
    )
    timeline = normalize_timeline_items(
        clusters_data,
        source_map=source_map,
        clusters=clusters,
        now=now,
        limit=limit_items,
    )
    source_rows = build_source_rows(
        sources=sources,
        latest_run=latest_run,
        health_report=health_report,
    )

    latest_payload = health_report.get("latest_run") or {}
    clustering_mode = clusters_data.get("clustering_mode") or "deterministic"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "disclaimer": DISCLAIMER,
        "clustering_mode": clustering_mode,
        "site": {
            "title": "AI Track News",
            "status_label": "M3a experimental Portal",
            "clustering_mode": clustering_mode,
            "experimental_note": experimental_note(clustering_mode),
        },
        "input": {
            "clusters_json": _relative(clusters_path),
            "clusters_run_id": clusters_data.get("run_id"),
            "clusters_generated_at": clusters_data.get("generated_at"),
            "snapshot_count": (clusters_data.get("input") or {}).get("snapshot_count"),
            "window_item_count": (clusters_data.get("input") or {}).get("window_item_count"),
            "candidate_cluster_count": (clusters_data.get("summary") or {}).get(
                "candidate_cluster_count"
            ),
        },
        "health": {
            "verdict": health_report.get("verdict"),
            "window": health_report.get("window"),
            "current_clean_streak": health_report.get("current_clean_streak"),
            "latest_run": latest_payload,
            "include_source_failures": health_report.get("include_source_failures", []),
        },
        "clusters": clusters,
        "timeline": timeline,
        "sources": source_rows,
    }


def find_latest_clusters_json(root: Path) -> Path | None:
    candidates = sorted(root.glob("**/clusters.json"))
    if not candidates:
        return None

    def sort_key(path: Path) -> tuple[str, str]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ("", str(path))
        generated_at = payload.get("generated_at")
        return (generated_at if isinstance(generated_at, str) else "", str(path))

    return max(candidates, key=sort_key)


def normalize_clusters(
    clusters_data: dict[str, Any],
    source_map: dict[str, Source],
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    raw_items = _dict_by_id(clusters_data.get("items"))
    clusters = []
    for index, raw_cluster in enumerate(_dicts(clusters_data.get("clusters"))[:limit], start=1):
        source_ids = _strings(raw_cluster.get("source_ids"))
        item_ids = _strings(raw_cluster.get("item_ids"))
        item_titles = [
            str(raw_items[item_id].get("title"))
            for item_id in item_ids
            if item_id in raw_items and raw_items[item_id].get("title")
        ]
        clusters.append(
            {
                "rank": index,
                "id": str(raw_cluster.get("id") or ""),
                "title": str(raw_cluster.get("title") or "Untitled cluster"),
                "heat_score": raw_cluster.get("heat_score"),
                "source_count": raw_cluster.get("source_count") or len(source_ids),
                "source_ids": source_ids,
                "source_names": [
                    source_label(source_id, source_map) for source_id in source_ids
                ],
                "topic_ids": _strings(raw_cluster.get("topic_ids")),
                "tier_mix": (
                    raw_cluster.get("tier_mix")
                    if isinstance(raw_cluster.get("tier_mix"), dict)
                    else {}
                ),
                "first_seen": raw_cluster.get("first_seen"),
                "last_seen": raw_cluster.get("last_seen"),
                "freshness": age_label(raw_cluster.get("last_seen"), now),
                "representative_url": str(raw_cluster.get("representative_url") or ""),
                "review_flags": _strings(raw_cluster.get("review_flags")),
                "item_ids": item_ids,
                "item_titles": item_titles[:6],
                "summary": raw_cluster.get("summary"),
                "experimental": bool(raw_cluster.get("experimental", True)),
            }
        )
    return clusters


def normalize_timeline_items(
    clusters_data: dict[str, Any],
    source_map: dict[str, Source],
    clusters: list[dict[str, Any]],
    now: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    cluster_by_item = {
        item_id: cluster
        for cluster in clusters
        for item_id in cluster.get("item_ids", [])
    }
    items = []
    for raw_item in _dicts(clusters_data.get("items")):
        item_id = str(raw_item.get("id") or "")
        event_at = item_time(raw_item)
        cluster = cluster_by_item.get(item_id)
        items.append(
            {
                "id": item_id,
                "source_id": str(raw_item.get("source_id") or ""),
                "source_name": source_label(str(raw_item.get("source_id") or ""), source_map),
                "title": str(raw_item.get("title") or "Untitled item"),
                "url": str(raw_item.get("url") or ""),
                "published_at": raw_item.get("published_at"),
                "fetched_at": raw_item.get("fetched_at"),
                "event_at": _iso(event_at) if event_at else None,
                "freshness": age_label(_iso(event_at) if event_at else None, now),
                "lang": str(raw_item.get("lang") or "unknown"),
                "topics": _strings(raw_item.get("topics")),
                "cluster_id": cluster.get("id") if cluster else None,
                "cluster_rank": cluster.get("rank") if cluster else None,
            }
        )
    return sorted(items, key=lambda item: item.get("event_at") or "", reverse=True)[:limit]


def build_source_rows(
    sources: list[Source],
    latest_run: RunRecord | None,
    health_report: dict[str, Any],
) -> list[dict[str, Any]]:
    latest_results = {}
    if latest_run is not None:
        latest_results = {
            str(row.get("source_id")): row
            for row in latest_run.source_results
            if row.get("source_id")
        }
    bad_counts = {
        str(row.get("source_id")): int(row.get("bad_runs") or 0)
        for row in health_report.get("include_source_failures", [])
        if isinstance(row, dict) and row.get("source_id")
    }
    rows = []
    sorted_sources = sorted(
        sources,
        key=lambda item: (item.m1_action != "include", item.tier, item.id),
    )
    for source in sorted_sources:
        latest = latest_results.get(source.id, {})
        item_count = _optional_int(latest.get("item_count"))
        status = str(latest.get("status") or "not_run")
        rows.append(
            {
                "id": source.id,
                "name": source.name,
                "tier": source.tier,
                "lang": source.lang,
                "access_method": source.access_method,
                "m1_action": source.m1_action,
                "tos_risk": source.tos_risk,
                "topics": source.topics,
                "notes": source.notes,
                "latest_status": status,
                "latest_item_count": item_count,
                "latest_error": latest.get("error"),
                "recent_bad_runs": bad_counts.get(source.id, 0),
                "health_state": source_health_state(status, item_count, source.m1_action),
            }
        )
    return rows


def write_site(portal: dict[str, Any], output_dir: Path) -> None:
    (output_dir / "assets").mkdir(parents=True, exist_ok=True)
    (output_dir / "sources").mkdir(parents=True, exist_ok=True)
    (output_dir / "data").mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_index_html(portal), encoding="utf-8")
    (output_dir / "sources" / "index.html").write_text(
        render_sources_html(portal),
        encoding="utf-8",
    )
    (output_dir / "assets" / "styles.css").write_text(STYLES, encoding="utf-8")
    (output_dir / "data" / "portal.json").write_text(
        json.dumps(portal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(output_dir / "index.html", output_dir / "404.html")


def render_index_html(portal: dict[str, Any]) -> str:
    health = portal["health"]
    latest_run = health.get("latest_run") or {}
    verdict = health.get("verdict") or {}
    latest_health = latest_run.get("health_ratio")
    source_count = latest_run.get("include_total")
    healthy_count = latest_run.get("healthy_include")
    clusters = portal["clusters"]
    timeline = portal["timeline"]
    input_meta = portal["input"]
    title = portal["site"]["title"]
    clustering_mode = portal.get("clustering_mode") or "deterministic"
    limits_html = "".join(f"<li>{h(item)}</li>" for item in known_limits(clustering_mode))
    return page_shell(
        title=title,
        active="home",
        body=f"""
        <header class="page-header">
          <div>
            <p class="eyebrow">M3a Portal</p>
            <h1>{h(title)}</h1>
            <p class="subtle">静态 AI 新闻雷达，基于已提交快照自动生成。</p>
          </div>
          <div class="status-panel">
            <span class="badge badge-warning">{h(experimental_badge(clustering_mode))}</span>
            <span class="status-line">
              Last updated <strong>{h(format_time(portal['generated_at']))}</strong>
            </span>
            <span class="status-line">
              M1 <strong>{h(str(verdict.get('status') or 'unknown'))}</strong>
              · {h(health_summary(healthy_count, source_count, latest_health))}
            </span>
          </div>
        </header>

        <section class="metrics-grid" aria-label="Operational status">
          {metric_card("Latest run", latest_run.get("run_id") or "-")}
          {metric_card("Window items", input_meta.get("window_item_count") or "-")}
          {metric_card("Candidate clusters", input_meta.get("candidate_cluster_count") or "-")}
          {metric_card("Snapshot count", input_meta.get("snapshot_count") or "-")}
        </section>

        <main class="content-grid">
          <section class="primary-stack" aria-labelledby="hot-clusters">
            <div class="section-heading">
              <div>
                <p class="eyebrow">Ranked by source weight × freshness</p>
                <h2 id="hot-clusters">当前热点</h2>
              </div>
              <a class="text-link" href="sources/">信源健康</a>
            </div>
            {render_clusters(clusters)}
          </section>

          <aside class="side-stack" aria-label="Collector status">
            <section class="panel">
              <h2>运行状态</h2>
              <dl class="detail-list">
                <div><dt>Health verdict</dt><dd>{h(str(verdict.get('status') or '-'))}</dd></div>
                <div><dt>Clean runs</dt><dd>{h(clean_run_summary(health))}</dd></div>
                <div><dt>Max gap</dt><dd>{h(max_gap_summary(health))}</dd></div>
                <div>
                  <dt>Cluster run</dt>
                  <dd>{h(str(input_meta.get('clusters_run_id') or '-'))}</dd>
                </div>
              </dl>
            </section>
            <section class="panel">
              <h2>已知限制</h2>
              <ul class="plain-list">
                {limits_html}
              </ul>
            </section>
          </aside>
        </main>

        <section class="timeline-section" id="timeline" aria-labelledby="timeline-title">
          <div class="section-heading">
            <div>
              <p class="eyebrow">Reverse chronological</p>
              <h2 id="timeline-title">全部 AI 动态</h2>
            </div>
          </div>
          {render_timeline(timeline)}
        </section>
        """,
    )


def render_sources_html(portal: dict[str, Any]) -> str:
    sources = portal["sources"]
    rows = "\n".join(render_source_row(source) for source in sources)
    health = portal["health"]
    verdict = health.get("verdict") or {}
    return page_shell(
        title="AI Track News · Sources",
        active="sources",
        asset_prefix="../",
        body=f"""
        <header class="page-header">
          <div>
            <p class="eyebrow">Source diet</p>
            <h1>信源健康</h1>
            <p class="subtle">运行清单来自 <code>config/sources.yml</code>，include 源进入 M1 gate。</p>
          </div>
          <div class="status-panel">
            <span class="badge {badge_class(str(verdict.get('status') or 'unknown'))}">
              M1 {h(str(verdict.get('status') or 'unknown'))}
            </span>
            <span class="status-line">
              Last updated <strong>{h(format_time(portal['generated_at']))}</strong>
            </span>
          </div>
        </header>

        <section class="source-table-wrap">
          <table class="source-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Tier</th>
                <th>Action</th>
                <th>Access</th>
                <th>Latest</th>
                <th>Bad runs</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {rows}
            </tbody>
          </table>
        </section>
        """,
    )


def page_shell(title: str, body: str, active: str, asset_prefix: str = "") -> str:
    home_active = "active" if active == "home" else ""
    sources_active = "active" if active == "sources" else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <link rel="stylesheet" href="{h(asset_prefix)}assets/styles.css">
</head>
<body>
  <div class="app-shell">
    <nav class="rail" aria-label="Primary">
      <a class="brand" href="{h(asset_prefix)}">AI Track</a>
      <a class="{home_active}" href="{h(asset_prefix)}">精选</a>
      <a class="{home_active}" href="{h(asset_prefix)}#timeline">全部动态</a>
      <a class="{sources_active}" href="{h(asset_prefix)}sources/">信源</a>
      <a href="{h(asset_prefix)}data/portal.json">数据</a>
    </nav>
    <div class="page">
      {body}
      <footer class="footer">{h(DISCLAIMER)}</footer>
    </div>
  </div>
</body>
</html>
"""


def render_clusters(clusters: list[dict[str, Any]]) -> str:
    if not clusters:
        return '<div class="empty-state">当前窗口没有聚类结果。</div>'
    return "\n".join(render_cluster_card(cluster) for cluster in clusters)


def render_cluster_card(cluster: dict[str, Any]) -> str:
    topics = "".join(chip(topic) for topic in cluster.get("topic_ids", []))
    flags = "".join(chip(flag, "flag-chip") for flag in cluster.get("review_flags", []))
    sources = ", ".join(cluster.get("source_names", [])) or "-"
    summary = cluster.get("summary")
    summary_html = f'<p class="cluster-summary">{h(summary)}</p>' if summary else ""
    item_lines = "".join(
        f"<li>{h(title)}</li>" for title in cluster.get("item_titles", [])[:4]
    )
    if not item_lines:
        item_lines = "<li>暂无条目明细</li>"
    return f"""
    <article class="cluster-card" id="{h(cluster['id'])}">
      <div class="cluster-rank">{h(str(cluster['rank']))}</div>
      <div class="cluster-main">
        <div class="cluster-meta">
          <span>Heat {h(str(cluster.get('heat_score') or '-'))}</span>
          <span>{h(str(cluster.get('source_count') or 0))} sources</span>
          <span>{h(str(cluster.get('freshness') or '-'))}</span>
        </div>
        <h3><a href="{h(cluster.get('representative_url') or '#')}">{h(cluster['title'])}</a></h3>
        <p class="source-line">{h(sources)}</p>
        {summary_html}
        <div class="chip-row">{topics}{flags}</div>
        <ul class="item-list">{item_lines}</ul>
      </div>
    </article>
    """


def render_timeline(timeline: list[dict[str, Any]]) -> str:
    if not timeline:
        return '<div class="empty-state">当前窗口没有 timeline 条目。</div>'
    return "\n".join(render_timeline_item(item) for item in timeline)


def render_timeline_item(item: dict[str, Any]) -> str:
    topics = "".join(chip(topic) for topic in item.get("topics", []))
    cluster_badge = ""
    if item.get("cluster_rank"):
        cluster_badge = f'<span class="badge">Cluster #{h(str(item["cluster_rank"]))}</span>'
    return f"""
    <article class="timeline-item">
      <div class="timeline-time">{h(str(item.get('freshness') or '-'))}</div>
      <div class="timeline-body">
        <div class="cluster-meta">
          <span>{h(str(item.get('source_name') or item.get('source_id') or '-'))}</span>
          {cluster_badge}
        </div>
        <h3>
          <a href="{h(str(item.get('url') or '#'))}">
            {h(str(item.get('title') or 'Untitled item'))}
          </a>
        </h3>
        <div class="chip-row">{topics}</div>
      </div>
    </article>
    """


def render_source_row(source: dict[str, Any]) -> str:
    status = str(source.get("latest_status") or "not_run")
    item_count = source.get("latest_item_count")
    latest = status if item_count is None else f"{status} · {item_count} items"
    topics = ", ".join(source.get("topics") or [])
    notes = source.get("notes") or ""
    return f"""
    <tr>
      <td><strong>{h(str(source['id']))}</strong><span>{h(str(source['name']))}</span></td>
      <td>Tier {h(str(source['tier']))}<span>{h(str(source.get('lang') or '-'))}</span></td>
      <td>
        <span class="badge {h(action_class(str(source['m1_action'])))}">
          {h(str(source['m1_action']))}
        </span>
      </td>
      <td>
        {h(str(source['access_method']))}
        <span>{h(str(source.get('tos_risk') or '-'))}</span>
      </td>
      <td><span class="health-dot {h(str(source['health_state']))}"></span>{h(latest)}</td>
      <td>{h(str(source.get('recent_bad_runs') or 0))}</td>
      <td>{h(notes)}<span>{h(topics)}</span></td>
    </tr>
    """


def metric_card(label: str, value: object) -> str:
    return f'<div class="metric-card"><span>{h(label)}</span><strong>{h(str(value))}</strong></div>'


def chip(value: object, css_class: str = "topic-chip") -> str:
    return f'<span class="{h(css_class)}">{h(str(value))}</span>'


def experimental_note(mode: str) -> str:
    if mode == "llm":
        return "LLM-assisted clustering (experimental); titles & summaries are AI-generated."
    if mode == "deterministic-fallback":
        return "Rules-only clustering (LLM unavailable; fallback)."
    return "Rules-only clustering; no LLM summaries."


def experimental_badge(mode: str) -> str:
    if mode == "llm":
        return "Experimental · LLM-assisted clustering"
    if mode == "deterministic-fallback":
        return "Experimental · rules-only (LLM fallback)"
    return "Experimental · rules-only clustering"


def known_limits(mode: str) -> list[str]:
    if mode == "llm":
        return [
            "聚类为 LLM 辅助实验态，未经人工审核。",
            "标题与摘由 GLM 生成，可能失真，请以原文为准。",
            "单源热点会被保留并明确标记。",
        ]
    return [
        "聚类仍是 deterministic rules-only。",
        "标题来自代表条目，非中文综合标题。",
        "单源热点会被保留并明确标记。",
    ]


def health_summary(healthy: object, total: object, ratio: object) -> str:
    if healthy is None or total is None:
        return "health unknown"
    suffix = ""
    if isinstance(ratio, (int, float)):
        suffix = f" · {ratio * 100:.2f}%"
    return f"{healthy}/{total} sources healthy{suffix}"


def clean_run_summary(health: dict[str, Any]) -> str:
    window = health.get("window") or {}
    clean = window.get("clean_runs")
    live = window.get("live_runs")
    if clean is None or live is None:
        return "-"
    return f"{clean}/{live}"


def max_gap_summary(health: dict[str, Any]) -> str:
    streak = health.get("current_clean_streak") or {}
    gap = streak.get("max_gap_hours")
    if isinstance(gap, (int, float)):
        return f"{gap:.2f}h"
    return "-"


def source_health_state(status: str, item_count: int | None, action: str) -> str:
    if action != "include" and status == "not_run":
        return "probe"
    if status == "success" and item_count and item_count > 0:
        return "healthy"
    if status == "success":
        return "zero"
    if status == "not_run":
        return "unknown"
    return "failed"


def badge_class(status: str) -> str:
    if status == "pass":
        return "badge-good"
    if status == "fail":
        return "badge-bad"
    return "badge-warning"


def action_class(action: str) -> str:
    return "badge-good" if action == "include" else "badge-muted"


def source_label(source_id: str, source_map: dict[str, Source]) -> str:
    source = source_map.get(source_id)
    return source.name if source else source_id


def item_time(item: dict[str, Any]) -> datetime | None:
    return _parse_time(item.get("published_at")) or _parse_time(item.get("fetched_at"))


def age_label(value: object, now: datetime) -> str:
    timestamp = _parse_time(value)
    if timestamp is None:
        return "-"
    delta = max(0.0, (_ensure_utc(now) - timestamp).total_seconds())
    minutes = int(delta // 60)
    if minutes < 90:
        return f"{minutes}m ago"
    hours = int(minutes // 60)
    if hours < 48:
        return f"{hours}h ago"
    days = int(hours // 24)
    return f"{days}d ago"


def format_time(value: object) -> str:
    timestamp = _parse_time(value)
    if timestamp is None:
        return str(value or "-")
    return timestamp.strftime("%Y-%m-%d %H:%M UTC")


def _dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_by_id(value: object) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("id")): item
        for item in _dicts(value)
        if item.get("id")
    }


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_cli_time(value: str) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid --now value: {value}")
    return parsed


def _parse_time(value: object) -> datetime | None:
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


def h(value: object) -> str:
    return escape(str(value), quote=True)


STYLES = """
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --panel-soft: #f1f5f4;
  --text: #17211d;
  --muted: #66736d;
  --line: #dce3df;
  --accent: #0f766e;
  --accent-soft: #d8f2ed;
  --warn: #8a5a00;
  --warn-soft: #fff2cc;
  --bad: #a33b32;
  --bad-soft: #fde4df;
  --good: #28734d;
  --good-soft: #dff3e8;
  font-family:
    Inter,
    ui-sans-serif,
    system-ui,
    -apple-system,
    BlinkMacSystemFont,
    "Segoe UI",
    sans-serif;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
}

a {
  color: inherit;
  text-decoration: none;
}

a:hover {
  color: var(--accent);
}

.app-shell {
  display: grid;
  grid-template-columns: 184px minmax(0, 1fr);
  min-height: 100vh;
}

.rail {
  position: sticky;
  top: 0;
  height: 100vh;
  padding: 22px 14px;
  border-right: 1px solid var(--line);
  background: #fbfcfc;
}

.rail a {
  display: block;
  padding: 9px 10px;
  border-radius: 8px;
  color: var(--muted);
  font-size: 14px;
}

.rail a.active,
.rail a:hover {
  background: var(--panel-soft);
  color: var(--text);
}

.rail .brand {
  margin-bottom: 22px;
  color: var(--text);
  font-weight: 750;
  font-size: 17px;
}

.page {
  width: min(1480px, 100%);
  padding: 28px;
}

.page-header {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: flex-start;
  margin-bottom: 22px;
}

.page-header h1 {
  margin: 0;
  font-size: 34px;
  line-height: 1.08;
  letter-spacing: 0;
}

.eyebrow {
  margin: 0 0 7px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 760;
  text-transform: uppercase;
}

.subtle {
  margin: 8px 0 0;
  color: var(--muted);
}

.status-panel {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 8px;
  color: var(--muted);
  font-size: 14px;
}

.badge,
.topic-chip,
.flag-chip {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 3px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--panel-soft);
  color: var(--muted);
  font-size: 12px;
  line-height: 1.3;
}

.badge-warning {
  border-color: #eddc9a;
  background: var(--warn-soft);
  color: var(--warn);
}

.badge-good {
  border-color: #b6dfc8;
  background: var(--good-soft);
  color: var(--good);
}

.badge-bad {
  border-color: #efbbb2;
  background: var(--bad-soft);
  color: var(--bad);
}

.badge-muted {
  background: #eef0f2;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 22px;
}

.metric-card,
.panel,
.cluster-card,
.timeline-item,
.source-table-wrap {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}

.metric-card {
  padding: 14px;
}

.metric-card span {
  display: block;
  color: var(--muted);
  font-size: 12px;
}

.metric-card strong {
  display: block;
  margin-top: 6px;
  font-size: 18px;
}

.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 18px;
}

.primary-stack,
.side-stack,
.timeline-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin: 6px 0 2px;
}

.section-heading h2,
.panel h2 {
  margin: 0;
  font-size: 20px;
  letter-spacing: 0;
}

.text-link {
  color: var(--accent);
  font-size: 14px;
}

.cluster-card {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr);
  gap: 14px;
  padding: 16px;
}

.cluster-rank {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 780;
}

.cluster-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  color: var(--muted);
  font-size: 12px;
}

.cluster-card h3,
.timeline-item h3 {
  margin: 8px 0;
  font-size: 18px;
  line-height: 1.28;
  letter-spacing: 0;
}

.source-line {
  margin: 0 0 9px;
  color: var(--muted);
  font-size: 13px;
}

.cluster-summary {
  margin: 0 0 9px;
  color: var(--text);
  font-size: 13.5px;
  line-height: 1.5;
}

.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.flag-chip {
  border-color: #d6cbc2;
  background: #f6efe9;
  color: #71513b;
}

.item-list,
.plain-list {
  margin: 10px 0 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 13px;
}

.item-list li + li,
.plain-list li + li {
  margin-top: 5px;
}

.panel {
  padding: 16px;
}

.detail-list {
  margin: 12px 0 0;
}

.detail-list div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 0;
  border-top: 1px solid var(--line);
}

.detail-list dt {
  color: var(--muted);
}

.detail-list dd {
  margin: 0;
  text-align: right;
  font-weight: 650;
}

.timeline-section {
  margin-top: 26px;
}

.timeline-item {
  display: grid;
  grid-template-columns: 82px minmax(0, 1fr);
  gap: 14px;
  padding: 14px 16px;
}

.timeline-time {
  color: var(--muted);
  font-size: 13px;
}

.source-table-wrap {
  overflow-x: auto;
}

.source-table {
  width: 100%;
  border-collapse: collapse;
  min-width: 980px;
}

.source-table th,
.source-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
  font-size: 13px;
}

.source-table th {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}

.source-table td span {
  display: block;
  margin-top: 4px;
  color: var(--muted);
}

.health-dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  margin-right: 8px;
  border-radius: 50%;
  background: var(--muted);
}

.health-dot.healthy {
  background: var(--good);
}

.health-dot.zero {
  background: var(--warn);
}

.health-dot.failed {
  background: var(--bad);
}

.health-dot.probe {
  background: #8b8f97;
}

.empty-state {
  padding: 18px;
  border: 1px dashed var(--line);
  border-radius: 8px;
  color: var(--muted);
  background: var(--panel);
}

.footer {
  margin-top: 34px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 13px;
}

code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.95em;
}

@media (max-width: 980px) {
  .app-shell {
    display: block;
  }

  .rail {
    position: static;
    display: flex;
    gap: 6px;
    height: auto;
    overflow-x: auto;
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }

  .rail .brand {
    margin: 0 12px 0 0;
    white-space: nowrap;
  }

  .page {
    padding: 18px;
  }

  .page-header,
  .content-grid {
    display: block;
  }

  .status-panel {
    align-items: flex-start;
    margin-top: 14px;
  }

  .metrics-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .side-stack {
    margin-top: 18px;
  }
}

@media (max-width: 620px) {
  .page-header h1 {
    font-size: 28px;
  }

  .metrics-grid,
  .cluster-card,
  .timeline-item {
    grid-template-columns: 1fr;
  }

  .cluster-rank {
    width: 34px;
    height: 34px;
  }
}
"""


if __name__ == "__main__":
    raise SystemExit(main())
