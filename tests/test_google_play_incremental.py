"""Verifica atualização incremental e publicação sem consultar a Google Play."""

import json
import fcntl
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import yaml

from scripts.collect import google_play as collector
from scripts.collect import google_play_storage as storage


def row(key, timestamp, score=5):
    return {"id_review": key, "data_avaliacao": pd.Timestamp(timestamp),
            "nota": score, "texto_avaliacao": "Exemplo"}


def upstream(key, timestamp, score=5):
    return {"reviewId": key, "at": datetime.fromisoformat(timestamp),
            "score": score, "content": "Exemplo"}


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = {"app_id": "example.bank", "name": "Example", "start_date": "2025-01-01"}
        self.settings = {
            "output_dir": str(self.root / "drive/raw"),
            "snapshot_dir": str(self.root / "drive/runs/snapshots"),
            "state_dir": str(self.root / "drive/runs/state"),
            "staging_dir": str(self.root / "local/staging"),
            "checkpoint_dir": str(self.root / "local/checkpoints"),
            "overlap_hours": 24, "batch_size": 500,
            "delay_seconds": 0, "checkpoint_every": 1, "max_retries": 1,
        }
        self.base = Path(self.settings["output_dir"]) / "example/reviews_raw.parquet"
        self.state = Path(self.settings["state_dir"]) / "example.json"
        self.snapshots = Path(self.settings["snapshot_dir"]) / "example"
        self.staging = Path(self.settings["staging_dir"]) / "example"
        self.historical = datetime(2025, 1, 1)
        self.base.parent.mkdir(parents=True)
        pd.DataFrame([row("historic", "2025-01-01"), row("latest", "2026-09-22T16:38:58")]).to_parquet(self.base, index=False)
        self.original_hash = storage.sha256(self.base)
        guard = patch.object(collector, "reviews", side_effect=AssertionError("Network access forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def reference(self):
        return storage.collection_reference(self.base, self.state, self.bank["app_id"], self.historical)

    def publish(self, records=None, reference=None):
        return storage.publish(pd.DataFrame(records or [row("new", "2026-10-05")]),
                               self.base, self.snapshots, self.staging, self.state,
                               self.bank["app_id"], self.historical,
                               reference or self.reference(), "2026-10-05T12:00:00")

    def fake_collect(self, pages):
        fetch = unittest.mock.Mock(side_effect=pages)
        with patch.object(collector, "make_fetch_batch", return_value=fetch):
            result = collector.collect_bank("example", self.bank, self.settings)
        return result, fetch

    def test_reference_uses_exactly_24_hours(self):
        ref = self.reference()
        self.assertEqual(ref["cutoff"], datetime(2026, 9, 21, 16, 38, 58))
        self.assertEqual(ref["origin"], "base")
        self.assertFalse(self.state.exists())

    def test_absent_base_uses_historical_start(self):
        self.base.unlink()
        self.assertEqual(self.reference()["cutoff"], self.historical)
        self.assertIsNone(self.reference()["base_sha256"])

    def test_merge_preserves_history_and_updates_existing_id(self):
        merged, state, snapshot = self.publish([row("latest", "2026-09-22T16:38:58", 1), row("new", "2026-10-05")])
        self.assertEqual(set(merged.id_review), {"historic", "latest", "new"})
        self.assertEqual(merged.set_index("id_review").at["latest", "nota"], 1)
        self.assertEqual(storage.sha256(snapshot), self.original_hash)
        self.assertEqual(storage.sha256(self.base), state["base_sha256"])
        self.assertEqual(json.loads(self.state.read_text()), state)
        self.assertEqual(self.reference()["origin"], "estado")
        self.assertEqual(self.reference()["cutoff"], datetime(2026, 10, 4))

    def test_malformed_state_is_rebuilt_without_advancing_it(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text('{"broken":')
        self.assertEqual(self.reference()["origin"], "base")
        self.assertEqual(self.state.read_text(), '{"broken":')

    def test_stale_hash_rebuilds_reference_from_base(self):
        self.publish()
        pd.DataFrame([row("other", "2026-10-03")]).to_parquet(self.base, index=False)
        self.assertEqual(self.reference()["origin"], "base")
        self.assertEqual(self.reference()["cutoff"], datetime(2026, 10, 2))

    def test_changed_base_cancels_publication(self):
        reference = self.reference()
        pd.DataFrame([row("other", "2026-10-03")]).to_parquet(self.base, index=False)
        changed_hash = storage.sha256(self.base)
        with self.assertRaisesRegex(RuntimeError, "mudou"):
            self.publish(reference=reference)
        self.assertEqual(storage.sha256(self.base), changed_hash)
        self.assertFalse(self.state.exists())

    def test_invalid_new_records_do_not_change_base_or_state(self):
        records = [row("bad", "2026-10-05", 7), row(None, "2026-10-05"),
                   {**row("bad", "2026-10-05"), "data_avaliacao": None}]
        for record in records:
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    self.publish([record])
                self.assertEqual(storage.sha256(self.base), self.original_hash)
                self.assertFalse(self.state.exists())

    def test_destination_copy_hash_mismatch_preserves_official_base(self):
        copy = storage.shutil.copy2
        def corrupt(source, destination):
            result = copy(source, destination)
            if Path(destination).name.endswith(".tmp.parquet"):
                Path(destination).write_bytes(b"incomplete")
            return result
        with patch.object(storage.shutil, "copy2", side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, "divergente"):
                self.publish()
        self.assertEqual(storage.sha256(self.base), self.original_hash)
        self.assertFalse(self.state.exists())
        self.assertFalse(list(self.base.parent.glob("*.tmp.parquet")))

    def test_snapshot_failure_preserves_official_base(self):
        copy = storage.shutil.copy2
        def fail_snapshot(source, destination):
            if "snapshots" in Path(destination).parts:
                raise OSError("Snapshot failed")
            return copy(source, destination)
        with patch.object(storage.shutil, "copy2", side_effect=fail_snapshot):
            with self.assertRaises(OSError):
                self.publish()
        self.assertEqual(storage.sha256(self.base), self.original_hash)
        self.assertFalse(self.state.exists())

    def test_incremental_collect_stops_at_boundary_and_processes_whole_batch(self):
        token = SimpleNamespace(token="next")
        pages = [([upstream("new", "2026-10-05"), upstream("boundary", "2026-09-21T16:38:58"),
                   upstream("older", "2026-09-21T16:38:57"), upstream("late_in_batch", "2026-09-22")], token)]
        (merged, report), fetch = self.fake_collect(pages)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(report["status"], "concluído")
        self.assertEqual(report["data_inicio_filtro"], "2026-09-21T16:38:58")
        self.assertEqual(set(merged.id_review), {"historic", "latest", "new", "boundary", "late_in_batch"})
        self.assertFalse(list(Path(self.settings["checkpoint_dir"]).glob("*_checkpoint*")))
        self.assertTrue(self.state.exists())

    def test_unconfirmed_coverage_preserves_base_and_local_checkpoint(self):
        token = SimpleNamespace(token="next")
        (_, report), _ = self.fake_collect([([upstream("new", "2026-10-05")], token), collector.CoverageUnconfirmed("Empty page")])
        self.assertEqual(report["status"], "cobertura não confirmada")
        self.assertEqual(storage.sha256(self.base), self.original_hash)
        self.assertFalse(self.state.exists())
        self.assertTrue((Path(self.settings["checkpoint_dir"]) / "example_checkpoint.parquet").exists())
        (merged, report), fetch = self.fake_collect([([upstream("older", "2026-09-20")], token)])
        self.assertEqual(report["status"], "concluído")
        self.assertEqual(fetch.call_args.args[2].token, "next")
        self.assertIn("new", set(merged.id_review))

    def test_failed_publication_can_resume_without_refetch(self):
        pages = [([upstream("new", "2026-10-05"), upstream("older", "2026-09-20")], None)]
        with patch.object(collector, "publish", side_effect=OSError("Drive unavailable")):
            with self.assertRaises(OSError):
                self.fake_collect(pages)
        self.assertEqual(storage.sha256(self.base), self.original_hash)
        (merged, report), fetch = self.fake_collect([])
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(report["status"], "concluído")
        self.assertIn("new", set(merged.id_review))

    def test_first_collection_reaches_historical_start(self):
        self.base.unlink()
        (merged, report), _ = self.fake_collect([([upstream("new", "2026-10-05"), upstream("older", "2024-12-31")], None)])
        self.assertEqual(report["modo"], "primeira coleta")
        self.assertEqual(set(merged.id_review), {"new"})
        self.assertTrue(self.state.exists())

    def test_fetch_without_token_requires_crossing_the_boundary(self):
        with patch.object(collector, "reviews", return_value=([upstream("new", "2026-10-05")], None)):
            with self.assertRaises(collector.CoverageUnconfirmed):
                collector.make_fetch_batch(1, datetime(2026, 9, 21))("example", 500, None)

    def test_checkpoint_identity_mismatch_preserves_base(self):
        token = SimpleNamespace(token="next")
        self.fake_collect([([upstream("new", "2026-10-05")], token), collector.CoverageUnconfirmed("Empty")])
        pd.DataFrame([row("changed", "2026-10-01")]).to_parquet(self.base, index=False)
        changed_hash = storage.sha256(self.base)
        with self.assertRaisesRegex(ValueError, "incompatível"):
            self.fake_collect([])
        self.assertEqual(storage.sha256(self.base), changed_hash)

    def test_staging_inside_official_storage_is_rejected(self):
        self.settings["staging_dir"] = str(self.base.parent / "staging")
        with self.assertRaisesRegex(ValueError, "fora do Drive"):
            collector.collect_bank("example", self.bank, self.settings)

    def test_state_write_failure_after_publication_is_recoverable(self):
        save = storage.save_json
        def fail_state(path, value):
            if Path(path) == self.state:
                raise OSError("State unavailable")
            return save(path, value)
        with patch.object(storage, "save_json", side_effect=fail_state):
            with self.assertRaises(OSError):
                self.publish()
        self.assertNotEqual(storage.sha256(self.base), self.original_hash)
        self.assertFalse(self.state.exists())
        self.assertTrue(storage.recover_publication(self.base, self.state, self.staging, self.bank["app_id"]))
        self.assertEqual(self.reference()["origin"], "estado")
        self.assertFalse((self.staging / "publication_pending.json").exists())

    def test_interruption_saves_latest_local_progress(self):
        self.settings["checkpoint_every"] = 5
        token = SimpleNamespace(token="next")
        with self.assertRaises(KeyboardInterrupt):
            self.fake_collect([([upstream("new", "2026-10-05")], token), KeyboardInterrupt()])
        self.assertEqual(storage.sha256(self.base), self.original_hash)
        checkpoint = Path(self.settings["checkpoint_dir"]) / "example_checkpoint.parquet"
        self.assertEqual(len(pd.read_parquet(checkpoint)), 1)

    def test_unavailable_official_symlink_is_not_treated_as_first_collection(self):
        link = self.root / "unavailable"
        link.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaisesRegex(FileNotFoundError, "indisponível"):
            storage.collection_reference(link / "example/reviews_raw.parquet", self.state,
                                         self.bank["app_id"], self.historical)

    def test_local_lock_prevents_simultaneous_collection(self):
        self.staging.mkdir(parents=True)
        with (self.staging / "collection.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "andamento"):
                collector.collect_bank("example", self.bank, self.settings)

    def test_dry_run_reports_boundary_without_writes_or_fetch(self):
        config = self.root / "config.yaml"
        config.write_text(yaml.safe_dump({"banks": {"example": self.bank}, "collection": self.settings}))
        output = io.StringIO()
        with patch("sys.argv", ["google_play", "--config", str(config), "--all", "--dry-run"]), redirect_stdout(output):
            collector.main()
        self.assertIn("2026-09-21T16:38:58", output.getvalue())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.staging.exists())
        self.assertEqual(storage.sha256(self.base), self.original_hash)

    def test_collection_recovers_state_then_accepts_a_new_checkpoint_identity(self):
        save = storage.save_json
        def fail_state(path, value):
            if Path(path) == self.state:
                raise OSError("State unavailable")
            return save(path, value)
        with patch.object(storage, "save_json", side_effect=fail_state):
            with self.assertRaises(OSError):
                self.fake_collect([([upstream("new", "2026-10-05"), upstream("older", "2026-09-20")], None)])
        (merged, report), _ = self.fake_collect([([upstream("newer", "2026-10-06"), upstream("boundary", "2026-10-03")], None)])
        self.assertEqual(report["data_inicio_filtro"], "2026-10-04T00:00:00")
        self.assertEqual(report["status"], "concluído")
        self.assertEqual(set(merged.id_review), {"historic", "latest", "new", "newer"})


if __name__ == "__main__":
    unittest.main()
