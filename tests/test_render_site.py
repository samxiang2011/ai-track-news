from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pipeline.render_site import build_portal, find_latest_clusters_json, write_site


class RenderSiteTests(unittest.TestCase):
    def test_render_escapes_titles_and_writes_expected_files(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = _fixture(root)
            portal = build_portal(
                clusters_path=paths["clusters"],
                manifest_root=paths["manifests"],
                sources_config=paths["sources"],
                now=_time("2026-06-23T13:00:00Z"),
                limit_clusters=5,
                limit_items=10,
            )

            write_site(portal, paths["site"])
            html = (paths["site"] / "index.html").read_text(encoding="utf-8")

            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertTrue((paths["site"] / "sources" / "index.html").exists())
            self.assertTrue((paths["site"] / "assets" / "styles.css").exists())
            self.assertTrue((paths["site"] / "data" / "portal.json").exists())
            self.assertTrue((paths["site"] / "404.html").exists())

    def test_portal_maps_clusters_to_timeline_and_source_health(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = _fixture(root)

            portal = build_portal(
                clusters_path=paths["clusters"],
                manifest_root=paths["manifests"],
                sources_config=paths["sources"],
                now=_time("2026-06-23T13:00:00Z"),
                limit_clusters=5,
                limit_items=10,
            )

        first_item = next(item for item in portal["timeline"] if item["id"] == "item-1")
        arxiv = next(source for source in portal["sources"] if source["id"] == "arxiv-cs-ai")

        self.assertEqual(first_item["cluster_rank"], 1)
        self.assertEqual(arxiv["latest_item_count"], 0)
        self.assertEqual(arxiv["recent_bad_runs"], 1)
        self.assertEqual(arxiv["health_state"], "zero")

    def test_render_is_deterministic_and_finds_latest_clusters(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = _fixture(root)
            older = root / "experimental" / "older" / "clusters.json"
            older.parent.mkdir(parents=True)
            older.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-22T00:00:00Z",
                        "clusters": [],
                        "items": [],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(find_latest_clusters_json(root / "experimental"), paths["clusters"])
            portal = build_portal(
                clusters_path=paths["clusters"],
                manifest_root=paths["manifests"],
                sources_config=paths["sources"],
                now=_time("2026-06-23T13:00:00Z"),
            )

        self.assertEqual(portal["generated_at"], "2026-06-23T13:00:00Z")
        self.assertEqual(portal["health"]["verdict"]["status"], "pass")


def _fixture(root: Path) -> dict[str, Path]:
    sources = root / "sources.yml"
    sources.write_text(
        """
sources:
  - id: openai-news
    name: OpenAI News
    tier: 1
    url: https://openai.com/news/rss.xml
    access_method: rss
    lang: en
    topics: [general-ai]
    tos_risk: low
    m1_action: include
    notes: Primary source.
  - id: arxiv-cs-ai
    name: arXiv cs.AI
    tier: 2
    url: https://export.arxiv.org/rss/cs.AI
    access_method: rss
    lang: en
    topics: [research]
    tos_risk: low
    m1_action: include
    notes: Research feed.
  - id: probe-source
    name: Probe Source
    tier: 3
    url: https://example.com
    access_method: public_page
    lang: en
    topics: [general-ai]
    tos_risk: low
    m1_action: probe
    notes: Probe only.
""".lstrip(),
        encoding="utf-8",
    )
    manifests = root / "manifests"
    (manifests / "2026-06").mkdir(parents=True)
    for day, hour in [
        (20, 12),
        (20, 18),
        (21, 0),
        (21, 6),
        (21, 12),
        (21, 18),
        (22, 0),
        (22, 6),
        (22, 12),
        (22, 18),
        (23, 0),
        (23, 6),
        (23, 12),
    ]:
        timestamp = f"2026-06-{day:02d}T{hour:02d}:00:00Z"
        run_id = f"202606{day:02d}T{hour:02d}0000Z-live"
        arxiv_count = 0 if day == 23 and hour == 12 else 1
        manifest = _manifest(run_id, timestamp, arxiv_count=arxiv_count)
        (manifests / "2026-06" / f"{run_id}.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
    clusters = root / "experimental" / "20260623T130000Z-m2-exp" / "clusters.json"
    clusters.parent.mkdir(parents=True)
    clusters.write_text(
        json.dumps(
            {
                "schema_version": "m2-experimental-v0",
                "run_id": "20260623T130000Z-m2-exp",
                "generated_at": "2026-06-23T13:00:00Z",
                "input": {
                    "snapshot_count": 1,
                    "deduped_item_count": 2,
                    "window_item_count": 2,
                },
                "summary": {
                    "cluster_count": 1,
                    "candidate_cluster_count": 1,
                    "experimental": True,
                    "llm_calls": 0,
                },
                "clusters": [
                    {
                        "id": "cluster-1",
                        "title": "Launch <script>alert(1)</script>",
                        "item_ids": ["item-1", "item-2"],
                        "source_ids": ["openai-news", "arxiv-cs-ai"],
                        "topic_ids": ["general-ai"],
                        "tier_mix": {"tier1": 1, "tier2": 1},
                        "source_count": 2,
                        "first_seen": "2026-06-23T11:00:00Z",
                        "last_seen": "2026-06-23T12:00:00Z",
                        "heat_score": 2.5,
                        "summary": None,
                        "representative_url": "https://example.com/cluster",
                        "review_flags": ["event_key_cluster"],
                        "experimental": True,
                    }
                ],
                "items": [
                    {
                        "id": "item-1",
                        "source_id": "openai-news",
                        "url": "https://example.com/item-1",
                        "title": "OpenAI announces launch",
                        "published_at": "2026-06-23T12:00:00Z",
                        "fetched_at": "2026-06-23T12:00:01Z",
                        "lang": "en",
                        "topics": ["general-ai"],
                    },
                    {
                        "id": "item-2",
                        "source_id": "arxiv-cs-ai",
                        "url": "https://example.com/item-2",
                        "title": "Research response",
                        "published_at": "2026-06-23T11:30:00Z",
                        "fetched_at": "2026-06-23T12:00:02Z",
                        "lang": "en",
                        "topics": ["research"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return {
        "sources": sources,
        "manifests": manifests,
        "clusters": clusters,
        "site": root / "site",
    }


def _manifest(run_id: str, timestamp: str, arxiv_count: int) -> dict[str, object]:
    healthy_include = 4 if arxiv_count == 0 else 5
    include_total = 5
    health_ratio = healthy_include / include_total
    return {
        "run_id": run_id,
        "started_at": timestamp,
        "finished_at": timestamp,
        "status": "success",
        "total_new_items": 2,
        "source_health": {
            "include_total": include_total,
            "healthy_include": healthy_include,
            "health_ratio": health_ratio,
            "gate_passed": True,
        },
        "source_results": [
            {
                "source_id": "openai-news",
                "status": "success",
                "item_count": 2,
                "error": None,
                "fetched_at": timestamp,
                "m1_action": "include",
            },
            {
                "source_id": "arxiv-cs-ai",
                "status": "success",
                "item_count": arxiv_count,
                "error": None,
                "fetched_at": timestamp,
                "m1_action": "include",
            },
        ],
        "runtime": {"github_actions": True},
    }


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


if __name__ == "__main__":
    unittest.main()
