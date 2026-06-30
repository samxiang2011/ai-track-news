"""Offline unit tests for pipeline.llm and pipeline.llm_smoke.

No network, no key. Deterministic. Live GLM behavior is verified by running
`python -m pipeline.llm_smoke` with LLM_API_KEY set, not by these tests.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from pipeline import llm, llm_smoke


EMPTY_ENV = {"LLM_API_KEY": "", "LLM_BASE_URL": "", "LLM_MODEL": ""}


class GLMClientTest(unittest.TestCase):
    @mock.patch.dict(os.environ, EMPTY_ENV, clear=False)
    def test_available_false_without_key(self):
        client = llm.GLMClient(api_key=None)
        self.assertFalse(client.available)

    @mock.patch.dict(os.environ, EMPTY_ENV, clear=False)
    def test_available_true_with_key(self):
        client = llm.GLMClient(api_key="fake-key", model="glm-test")
        self.assertTrue(client.available)

    @mock.patch.dict(os.environ, EMPTY_ENV, clear=False)
    def test_base_url_strips_trailing_slash(self):
        client = llm.GLMClient(api_key="k", base_url="https://example.com/api/v4/", model="m")
        self.assertEqual(client.base_url, "https://example.com/api/v4")

    @mock.patch.dict(os.environ, EMPTY_ENV, clear=False)
    def test_chat_json_requires_json_keyword_in_prompt(self):
        client = llm.GLMClient(api_key="k", model="m")
        with self.assertRaises(llm.LLMError):
            client.chat_json("classify this", "items here")

    @mock.patch.dict(os.environ, EMPTY_ENV, clear=False)
    def test_default_model_when_unset(self):
        # With no explicit model and empty env, the client self-provides the
        # Coding-Plan default (glm-5.2) so callers never depend on live
        # model discovery.
        client = llm.GLMClient(api_key="k", model=None)
        self.assertEqual(client.model, llm.DEFAULT_MODEL)

    @mock.patch.dict(os.environ, EMPTY_ENV, clear=False)
    def test_chat_json_parses_content_and_tracks_usage(self):
        client = llm.GLMClient(api_key="k", model="glm-test")
        canned = {
            "content": [{"type": "text", "text": '{"results": []}'}],
            "usage": {"input_tokens": 40, "output_tokens": 5},
        }
        with mock.patch.object(client, "_request", return_value=canned):
            parsed, raw = client.chat_json("return JSON only", "items")
        self.assertEqual(parsed, {"results": []})
        self.assertEqual(client.usage.calls, 1)
        self.assertEqual(client.usage.prompt_tokens, 40)
        self.assertEqual(client.usage.completion_tokens, 5)

    def test_extract_content_rejects_empty(self):
        with self.assertRaises(llm.LLMError):
            llm._extract_content({"content": [{"type": "text", "text": ""}]})


class PickModelTest(unittest.TestCase):
    def test_prefers_glm_chat_id(self):
        models = [
            {"id": "embedding-2"},
            {"id": "glm-4.6"},
            {"id": "cogview-3"},
        ]
        self.assertEqual(llm.pick_model(models), "glm-4.6")

    def test_prefers_decided_glm51_over_46(self):
        models = [{"id": "glm-4.6"}, {"id": "glm-5.1"}, {"id": "glm-5.2"}]
        self.assertEqual(llm.pick_model(models), "glm-5.1")

    def test_avoids_non_chat_skus(self):
        models = [{"id": "embedding-3"}, {"id": "cogview-4"}, {"id": "glm-4.6"}]
        self.assertEqual(llm.pick_model(models), "glm-4.6")

    def test_returns_none_when_empty(self):
        self.assertIsNone(llm.pick_model([]))


class LoadRecentItemsTest(unittest.TestCase):
    def _write_snapshot(self, dir_path: Path, items: list[dict], name: str = "test.jsonl") -> Path:
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / name
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return path

    def test_window_dedup_and_cap(self):
        now = datetime.now(timezone.utc)
        recent_a = now - timedelta(hours=2)
        recent_b = now - timedelta(hours=5)
        stale = now - timedelta(hours=100)
        iso = lambda dt: dt.isoformat().replace("+00:00", "Z")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_snapshot(
                root / "2026-06",
                [
                    {"id": "a", "source_id": "s1", "title": "A", "fetched_at": iso(recent_a)},
                    {"id": "a", "source_id": "s1", "title": "A-dup", "fetched_at": iso(recent_b)},
                    {"id": "stale", "source_id": "s1", "title": "old", "fetched_at": iso(stale)},
                ],
            )
            self._write_snapshot(
                root / "2026-06",
                [{"id": "b", "source_id": "s2", "title": "B", "fetched_at": iso(recent_b)}],
                name="test2.jsonl",
            )
            loaded = llm_smoke.load_recent_items(root, now, window_hours=48.0, limit=10)
        ids = [it["id"] for it in loaded]
        self.assertEqual(ids, ["a", "b"])  # deduped, newest first, stale excluded
        # dedup keeps the most recent fetch
        self.assertEqual(loaded[0]["title"], "A")

    def test_limit_caps_results(self):
        now = datetime.now(timezone.utc)
        iso = now.isoformat().replace("+00:00", "Z")
        items = [
            {"id": f"id{i}", "source_id": "s", "title": f"T{i}", "fetched_at": iso}
            for i in range(10)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_snapshot(root / "m", items)
            loaded = llm_smoke.load_recent_items(root, now, window_hours=48.0, limit=3)
        self.assertEqual(len(loaded), 3)


class CountMultiSourceTest(unittest.TestCase):
    def test_counts_only_cross_source_clusters(self):
        items = [
            {"id": "1", "source_id": "openai"},
            {"id": "2", "source_id": "techcrunch"},
            {"id": "3", "source_id": "openai"},
            {"id": "4", "source_id": "hnrss"},
        ]
        clusters = [
            {"item_ids": ["1", "2"]},  # multi-source
            {"item_ids": ["1", "3"]},  # single source (both openai)
            {"item_ids": ["4"]},       # single
        ]
        self.assertEqual(llm_smoke._count_multi_source(clusters, items), 1)

    def test_ignores_unknown_ids(self):
        items = [{"id": "1", "source_id": "openai"}]
        clusters = [{"item_ids": ["1", "ghost"]}]
        self.assertEqual(llm_smoke._count_multi_source(clusters, items), 0)


class MockRunTest(unittest.TestCase):
    def test_mock_produces_a_multi_source_cluster(self):
        items = [
            {"id": f"id{i}", "source_id": src}
            for i, src in enumerate(["openai", "techcrunch", "hnrss", "decoder"])
        ]
        result = llm_smoke._mock(items, {})
        self.assertEqual(result["discovery"]["note"], "mock mode; no key, no live call")
        multi = llm_smoke._count_multi_source(result["clusters"], items)
        self.assertGreaterEqual(multi, 1)


if __name__ == "__main__":
    unittest.main()
