from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pipeline.run_m2_experimental import (
    M2Item,
    SourceMeta,
    TopicRule,
    build_clusters,
    choose_representative,
    item_from_raw,
    load_snapshot_items,
    merge_topics,
    select_review_clusters,
    score_heat,
    should_cluster_items,
    should_cluster_titles,
)


class M2ExperimentalTests(unittest.TestCase):
    def test_title_similarity_clusters_related_titles(self) -> None:
        left = _item("a", "openai-news", "OpenAI launches o3-pro for developers", tier=1)
        right = _item("b", "github-ai-blog", "Developers get OpenAI o3-pro launch", tier=1)

        self.assertTrue(should_cluster_titles(left, right))

    def test_event_key_clusters_fable_titles(self) -> None:
        left = _item(
            "a",
            "techcrunch-ai",
            "Anthropic releases Claude Fable 5 and Mythos 5",
            tier=3,
        )
        right = _item(
            "b",
            "the-decoder",
            "Claude Fable 5: The first Mythos model is powerful and filtered",
            tier=3,
        )

        self.assertTrue(should_cluster_items(left, right))

    def test_event_key_does_not_cluster_generated_with_example(self) -> None:
        left = _item(
            "a",
            "techcrunch-ai",
            "Anthropic releases Claude Fable 5 and Mythos 5",
            tier=3,
        )
        right = _item(
            "b",
            "hnrss-ai",
            "Show HN: Pacman AI - Generated with Claude Fable 5",
            tier=2,
        )

        self.assertFalse(should_cluster_items(left, right))

    def test_representative_prefers_central_title_over_hn_edge_title(self) -> None:
        release = _item(
            "a",
            "the-decoder",
            "Anthropic releases Claude Fable 5 and Mythos 5",
            tier=3,
        )
        overview = _item(
            "b",
            "techcrunch-ai",
            "Anthropic's Claude Fable 5 is a version of Mythos",
            tier=3,
        )
        edge = _item(
            "c",
            "hnrss-ai",
            "AI researcher claims he's bypassed Anthropic's Fable 5 guardrails",
            tier=2,
        )

        representative = choose_representative([release, overview, edge])

        self.assertNotEqual(representative.source_id, "hnrss-ai")

    def test_cross_snapshot_item_consolidation_uses_id_or_url(self) -> None:
        with TemporaryDirectory() as tmpdir:
            first_path = Path(tmpdir) / "first-live.jsonl"
            second_path = Path(tmpdir) / "second-live.jsonl"
            first_path.write_text(
                json.dumps(
                    _raw_item(
                        "a",
                        "openai-news",
                        "https://example.com/a",
                        "OpenAI launches o3-pro for developers",
                        fetched_at="2026-06-11T10:00:00Z",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            second_path.write_text(
                json.dumps(
                    _raw_item(
                        "different-id",
                        "openai-news",
                        "https://example.com/a",
                        "OpenAI launches o3-pro for developers",
                        fetched_at="2026-06-11T12:00:00Z",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            items = load_snapshot_items(
                [first_path, second_path],
                {"openai-news": SourceMeta("openai-news", 1, ())},
                [],
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "a")
        self.assertEqual(items[0].fetched_at, "2026-06-11T10:00:00Z")

    def test_topic_keyword_matching_adds_token_export(self) -> None:
        rule = TopicRule(
            id="token-export",
            source_hints=(),
            keywords_zh=(),
            keywords_en=("openai-compatible", "model gateway"),
            company_hints=("qwen",),
        )
        topics = merge_topics(
            ["general-ai"],
            ("platform",),
            raw={
                "title": "Qwen support lands in OpenAI-compatible model gateway",
                "url": "https://example.com/post",
                "excerpt": None,
            },
            source_id="example",
            topic_rules=[rule],
        )

        self.assertEqual(topics, ["general-ai", "platform", "token-export"])

    def test_company_hint_does_not_trigger_topic_by_itself(self) -> None:
        rule = TopicRule(
            id="token-export",
            source_hints=(),
            keywords_zh=(),
            keywords_en=("openai-compatible",),
            company_hints=("qwen",),
        )
        topics = merge_topics(
            ["general-ai"],
            (),
            raw={
                "title": "Qwen releases a new chat model",
                "url": "https://example.com/post",
                "excerpt": None,
            },
            source_id="example",
            topic_rules=[rule],
        )

        self.assertEqual(topics, ["general-ai"])

    def test_weak_benchmark_keyword_does_not_trigger_token_export(self) -> None:
        rule = TopicRule(
            id="token-export",
            source_hints=(),
            keywords_zh=(),
            keywords_en=("benchmark", "model gateway"),
            company_hints=("qwen",),
        )
        topics = merge_topics(
            ["research"],
            (),
            raw={
                "title": "Benchmarking Large Language Models for Safety Data Extraction",
                "url": "https://example.com/post",
                "excerpt": None,
            },
            source_id="example",
            topic_rules=[rule],
        )

        self.assertEqual(topics, ["research"])

    def test_select_review_clusters_caps_single_source_items(self) -> None:
        clusters = [
            _cluster("a", ["hnrss-ai"]),
            _cluster("b", ["hnrss-ai"]),
            _cluster("c", ["openai-news"]),
            _cluster("d", ["techcrunch-ai"]),
            _cluster("e", ["hnrss-ai", "techcrunch-ai"], item_ids=["e", "f"]),
        ]

        selected = select_review_clusters(clusters, limit=5, max_clusters_per_source=1)

        self.assertEqual([cluster["id"] for cluster in selected], ["a", "c", "d", "e"])

    def test_heat_score_orders_multi_source_recent_cluster_first(self) -> None:
        now = _time("2026-06-11T12:00:00Z")
        recent_multi = [
            _item("a", "openai-news", "OpenAI launches o3-pro for developers", tier=1),
            _item("b", "github-ai-blog", "Developers get OpenAI o3-pro launch", tier=1),
        ]
        old_single = [
            _item(
                "c",
                "techcrunch-ai",
                "Startup releases AI assistant",
                tier=3,
                published_at="2026-06-09T12:00:00Z",
            )
        ]

        self.assertGreater(
            score_heat(recent_multi, now=now),
            score_heat(old_single, now=now),
        )

    def test_item_from_raw_uses_source_topics_and_first_fetched_fallback(self) -> None:
        item = item_from_raw(
            {
                "id": "a",
                "source_id": "source",
                "url": "https://example.com/a",
                "title": "Example title",
                "published_at": None,
                "fetched_at": "2026-06-11T12:00:00Z",
                "lang": "en",
                "topics": ["general-ai"],
                "excerpt": None,
            },
            {"source": SourceMeta("source", 2, ("developer-tools",))},
            [],
        )

        self.assertEqual(item.event_at, _time("2026-06-11T12:00:00Z"))
        self.assertEqual(item.topics, ["developer-tools", "general-ai"])


def _item(
    item_id: str,
    source_id: str,
    title: str,
    tier: int,
    published_at: str = "2026-06-11T11:00:00Z",
) -> M2Item:
    raw = _raw_item(
        item_id,
        source_id,
        f"https://example.com/{item_id}",
        title,
        published_at=published_at,
    )
    source_meta = {source_id: SourceMeta(source_id, tier, ())}
    return item_from_raw(raw, source_meta, [])


def _raw_item(
    item_id: str,
    source_id: str,
    url: str,
    title: str,
    published_at: str | None = "2026-06-11T11:00:00Z",
    fetched_at: str = "2026-06-11T12:00:00Z",
) -> dict[str, object]:
    return {
        "id": item_id,
        "source_id": source_id,
        "url": url,
        "title": title,
        "published_at": published_at,
        "fetched_at": fetched_at,
        "lang": "en",
        "topics": ["general-ai"],
        "excerpt": None,
    }


def _cluster(
    cluster_id: str,
    source_ids: list[str],
    item_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": cluster_id,
        "source_ids": source_ids,
        "item_ids": item_ids or [cluster_id],
        "heat_score": 1.0,
        "last_seen": "2026-06-11T12:00:00Z",
    }


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


if __name__ == "__main__":
    unittest.main()
