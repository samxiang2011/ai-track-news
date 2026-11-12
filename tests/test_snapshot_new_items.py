from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pipeline import run_m1
from pipeline.normalize import Item
from pipeline.storage import load_seen_item_ids, select_new_items, write_snapshot


def _item(item_id: str, title: str = "title") -> Item:
    return Item(
        id=item_id,
        source_id="src",
        url=f"https://example.com/{item_id}",
        title=title,
        published_at=None,
        fetched_at="2026-11-12T00:00:00Z",
        lang="en",
        excerpt=None,
        topics=[],
        cluster_id=None,
        origin_url=None,
    )


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


class LoadSeenItemIdsTest(unittest.TestCase):
    def test_missing_snapshot_root_means_nothing_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_seen_item_ids(Path(tmp)), set())

    def test_reads_ids_across_months_from_committed_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_lines(
                root / "data/snapshots/2026-10/a-live.jsonl",
                [json.dumps({"id": "one"}), json.dumps({"id": "two"})],
            )
            _write_lines(
                root / "data/snapshots/2026-11/b-live.jsonl",
                [json.dumps({"id": "two"}), json.dumps({"id": "three"})],
            )
            self.assertEqual(load_seen_item_ids(root), {"one", "two", "three"})

    def test_ignores_dry_run_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_lines(
                root / "data/dry-runs/snapshots/2026-11/a-dry.jsonl",
                [json.dumps({"id": "dry"})],
            )
            self.assertEqual(load_seen_item_ids(root), set())

    def test_skips_damaged_lines_and_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_lines(
                root / "data/snapshots/2026-11/a-live.jsonl",
                ["not json", "[1, 2]", json.dumps({"title": "no id"}), json.dumps({"id": "ok"})],
            )
            _write_lines(root / "data/snapshots/2026-11/b-live.jsonl", [])
            self.assertEqual(load_seen_item_ids(root), {"ok"})


class SelectNewItemsTest(unittest.TestCase):
    def test_keeps_only_unseen_items_in_order(self) -> None:
        items = [_item("a"), _item("b"), _item("c")]
        self.assertEqual(
            [item.id for item in select_new_items(items, {"b"})], ["a", "c"]
        )

    def test_same_id_with_changed_title_is_not_new(self) -> None:
        self.assertEqual(select_new_items([_item("a", title="edited")], {"a"}), [])

    def test_written_snapshot_feeds_the_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = select_new_items([_item("a"), _item("b")], load_seen_item_ids(root))
            write_snapshot(root, "run1-live", first, dry_run=False)
            second = select_new_items(
                [_item("b"), _item("c")], load_seen_item_ids(root)
            )
            path = write_snapshot(root, "run2-live", second, dry_run=False)
            self.assertEqual([item.id for item in first], ["a", "b"])
            self.assertEqual(
                [json.loads(line)["id"] for line in path.read_text().splitlines()],
                ["c"],
            )
            third = select_new_items([_item("c")], load_seen_item_ids(root))
            empty = write_snapshot(root, "run3-live", third, dry_run=False)
            self.assertEqual(empty.read_text(), "")


class DryRunManifestTest(unittest.TestCase):
    def test_dry_run_keeps_full_window_and_reports_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(run_m1, "ROOT", root), mock.patch("builtins.print"):
                self.assertEqual(run_m1.main(["--dry-run"]), 0)
                self.assertEqual(run_m1.main(["--dry-run"]), 0)
            manifests = sorted((root / "data/dry-runs/manifests").glob("*/*.json"))
            self.assertTrue(manifests)
            for path in manifests:
                manifest = json.loads(path.read_text())
                self.assertEqual(manifest["snapshot_mode"], "full_window")
                self.assertEqual(
                    manifest["snapshot_item_count"], manifest["total_new_items"]
                )
                self.assertGreater(manifest["total_new_items"], 0)
            self.assertFalse((root / "data/snapshots").exists())


class LiveRunSnapshotTest(unittest.TestCase):
    def test_second_live_run_writes_no_repeats_and_keeps_window_count(self) -> None:
        real_fetch = run_m1.fetch_source

        def synthetic_fetch(source, timeout, dry_run):  # noqa: ANN001, ANN202
            return real_fetch(source, timeout=timeout, dry_run=True)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(run_m1, "ROOT", root), mock.patch.object(
                run_m1, "fetch_source", synthetic_fetch
            ), mock.patch.object(
                run_m1, "_run_id", side_effect=["run1-live", "run2-live"]
            ), mock.patch("builtins.print"):
                self.assertEqual(run_m1.main([]), 0)
                self.assertEqual(run_m1.main([]), 0)

            def manifest(run_id: str) -> dict[str, object]:
                (path,) = (root / "data/manifests").glob(f"*/{run_id}.json")
                return json.loads(path.read_text())

            def snapshot_lines(run_id: str) -> list[str]:
                (path,) = (root / "data/snapshots").glob(f"*/{run_id}.jsonl")
                return path.read_text().splitlines()

            first, second = manifest("run1-live"), manifest("run2-live")
            window = first["total_new_items"]
            self.assertGreater(window, 0)
            self.assertEqual(first["snapshot_mode"], "new_items_only")
            self.assertEqual(first["snapshot_item_count"], window)
            self.assertEqual(len(snapshot_lines("run1-live")), window)
            # Same feeds again: nothing is new, but the health-facing numbers hold.
            self.assertEqual(second["total_new_items"], window)
            self.assertEqual(second["snapshot_item_count"], 0)
            self.assertEqual(snapshot_lines("run2-live"), [])
            self.assertEqual(
                [row["item_count"] for row in first["source_results"]],
                [row["item_count"] for row in second["source_results"]],
            )
            self.assertEqual(first["source_health"], second["source_health"])
            self.assertFalse((root / "data/dry-runs").exists())


if __name__ == "__main__":
    unittest.main()
