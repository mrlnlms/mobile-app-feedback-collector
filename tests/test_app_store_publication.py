"""Falhas de publicação, retomada e isolamento local sem consultar a App Store."""

import fcntl
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yaml

from scripts.collect import app_store as collector
from scripts.collect import app_store_storage as storage
from scripts.collect.google_play_storage import sha256
from scripts.probe.app_store import run_probe


def entry(key="new", score=5):
    return {"id": {"label": key}, "updated": {"label": "2026-10-05T00:00:00Z"},
            "im:rating": {"label": str(score)}, "content": {"label": "Teste"}}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = {"apple_app_id": "123", "name": "Example", "start_date": "2025-01-01"}
        self.settings = {"country": "br", "sorts": ["mostrecent"], "max_pages_per_sort": 1,
                         "delay_seconds": 0, "max_retries": 1,
                         **{k: str(self.root / v) for k, v in {
                             "output_dir": "drive/raw", "snapshot_dir": "drive/runs/snapshots",
                             "state_dir": "drive/runs/state", "report_dir": "drive/runs/reports",
                             "staging_dir": "local/staging", "checkpoint_dir": "local/checkpoints"}.items()}}
        self.base = Path(self.settings["output_dir"]) / "example/reviews_raw.parquet"
        self.base.parent.mkdir(parents=True)
        old = collector.parse_review(entry("old"), "123", "br", "mostrecent")
        old["data_avaliacao"] = pd.Timestamp("2025-01-01T00:00:00Z")
        pd.DataFrame([old], columns=collector.COLS).to_parquet(self.base, index=False)
        self.before = sha256(self.base)
        self.checkpoint = Path(self.settings["checkpoint_dir"]) / "example_checkpoint.json"
        self.staging = Path(self.settings["staging_dir"]) / "example"
        self.state = Path(self.settings["state_dir"]) / "example.json"
        guard = patch.object(collector, "fetch_page", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def collect(self, entries=None):
        with patch.object(collector, "fetch_page", return_value=entries if entries is not None else [entry()]) as fetch:
            report = collector.collect_bank("example", self.bank, self.settings)
        return report, fetch

    def snapshots(self):
        return list(Path(self.settings["snapshot_dir"]).glob("example/*.parquet"))

    def test_success_preserves_history_updates_same_id_and_cleans_local_files(self):
        report, _ = self.collect([entry("old", 1), entry()])
        frame = pd.read_parquet(self.base).set_index("id_review")
        self.assertEqual(set(frame.index), {"old", "new"})
        self.assertEqual(frame.at["old", "nota"], 1)
        self.assertEqual(sha256(self.snapshots()[0]), self.before)
        self.assertEqual(report["base_sha256"], sha256(self.base))
        self.assertEqual(json.loads(self.state.read_text())["reviews_in_base"], 2)
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse((self.staging / "reviews_merged.parquet").exists())
        self.assertFalse((self.staging / "publication_pending.json").exists())

    def test_copy_corruption_preserves_base_and_checkpoint(self):
        original_copy = shutil.copy2
        def corrupt(source, target):
            result = original_copy(source, target)
            if str(target).endswith('.tmp.parquet'):
                Path(target).write_bytes(b'corrupted')
            return result
        with patch.object(storage.shutil, "copy2", side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                self.collect()
        self.assertEqual(sha256(self.base), self.before)
        self.assertTrue(self.checkpoint.exists())
        self.assertFalse(self.snapshots())
        report, fetch = self.collect()
        fetch.assert_not_called()
        self.assertEqual(report["status"], "concluído")

    def test_snapshot_failure_preserves_base(self):
        original_copy = shutil.copy2
        def fail(source, target):
            if 'before_recollect' in str(target):
                raise OSError('snapshot unavailable')
            return original_copy(source, target)
        with patch.object(storage.shutil, "copy2", side_effect=fail):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(sha256(self.base), self.before)
        self.assertFalse(self.snapshots())
        self.assertTrue(self.checkpoint.exists())

    def test_state_failure_recovers_without_refetch_or_second_snapshot(self):
        original_save = storage.save_json
        def fail(path, value):
            if Path(path) == self.state:
                raise OSError('state unavailable')
            return original_save(path, value)
        with patch.object(storage, "save_json", side_effect=fail):
            with self.assertRaises(OSError):
                self.collect()
        published = sha256(self.base)
        self.assertNotEqual(published, self.before)
        self.assertTrue(self.checkpoint.exists())
        self.assertTrue((self.staging / 'publication_pending.json').exists())
        report, fetch = self.collect()
        fetch.assert_not_called()
        self.assertEqual(sha256(self.base), published)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(report['base_sha256'], published)
        self.assertFalse((self.staging / 'reviews_merged.parquet').exists())

    def test_base_changed_during_fetch_is_not_overwritten(self):
        def changed(*args):
            frame = pd.read_parquet(self.base)
            frame.loc[0, 'nota'] = 2
            frame.to_parquet(self.base, index=False)
            return [entry()]
        with patch.object(collector, 'fetch_page', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'mudou'):
                collector.collect_bank('example', self.bank, self.settings)
        self.assertEqual(pd.read_parquet(self.base).nota.tolist(), [2])
        self.assertTrue(self.checkpoint.exists())

    def test_empty_feeds_preserve_previous_base(self):
        with self.assertRaisesRegex(RuntimeError, 'Nenhuma review'):
            self.collect([])
        self.assertEqual(sha256(self.base), self.before)
        self.assertTrue(self.checkpoint.exists())
        self.assertFalse(self.state.exists())

    def test_empty_feed_can_be_retried_with_the_same_command(self):
        with self.assertRaises(RuntimeError):
            self.collect([])
        report, fetch = self.collect()
        fetch.assert_called_once()
        self.assertEqual(report['reviews_na_base'], 2)

    def test_cli_publishes_report_and_updates_manifest_automatically(self):
        google = self.root / 'google/example/reviews_raw.parquet'
        google.parent.mkdir(parents=True)
        pd.DataFrame([{'id_review': 'g', 'nota': 5, 'data_avaliacao': pd.Timestamp('2025-01-01')}]).to_parquet(google, index=False)
        config = {'banks': {'example': self.bank}, 'app_store': self.settings,
                  'collection': {'output_dir': str(google.parent.parent),
                                 'snapshot_dir': str(self.root / 'google/snapshots')}}
        manifest = self.root / 'manifest.json'
        self.settings['manifest_file'] = str(manifest)
        config_path = self.root / 'config.yaml'
        config['app_store']['probe_dir'] = str(self.root / 'experiments')
        config_path.write_text(yaml.safe_dump(config))
        with patch.object(sys, 'argv', ['app_store', '--all', '--config', str(config_path)]), patch.object(collector, 'fetch_page', return_value=[entry()]):
            self.assertEqual(collector.main(), 0)
        files = json.loads(manifest.read_text())['files']
        primary = [f for f in files if f['platform'] == 'app_store' and f['kind'] == 'primary'][0]
        self.assertEqual(primary['sha256'], sha256(self.base))
        self.assertEqual(primary['reviews'], 2)
        self.assertEqual(len(list(Path(self.settings['report_dir']).glob('report_*.json'))), 1)

    def test_unavailable_symlink_fails_before_fetch(self):
        link = self.root / 'unavailable'
        link.symlink_to(self.root / 'missing-drive', target_is_directory=True)
        self.settings['output_dir'] = str(link)
        with self.assertRaisesRegex(FileNotFoundError, 'indisponível'):
            collector.collect_bank('example', self.bank, self.settings)
        self.assertFalse((self.root / 'missing-drive').exists())

    def test_checkpoint_on_drive_is_rejected(self):
        self.settings['checkpoint_dir'] = str(self.root / 'CloudStorage/checkpoints')
        with self.assertRaisesRegex(ValueError, 'fora do Drive'):
            collector.collect_bank('example', self.bank, self.settings)

    def test_local_lock_prevents_parallel_collection(self):
        self.staging.mkdir(parents=True)
        with (self.staging / 'collection.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'andamento'):
                collector.collect_bank('example', self.bank, self.settings)

    def test_probe_with_drive_output_keeps_work_local_and_original_unchanged(self):
        config = {'banks': {'example': self.bank}, 'app_store': self.settings}
        destination = self.root / 'CloudStorage/experiments'
        with patch.object(collector, 'fetch_page', return_value=[entry()]):
            run = run_probe('example', config, destination)
        self.assertEqual(sha256(self.base), self.before)
        self.assertTrue((run / 'probe.json').exists())
        self.assertEqual(len(pd.read_parquet(run / 'example/reviews_raw.parquet')), 1)
        self.assertFalse((run / 'checkpoints').exists())

    def test_empty_probe_preserves_canonical_base_and_publishes_empty_experiment(self):
        config = {'banks': {'example': self.bank}, 'app_store': self.settings}
        with patch.object(collector, 'fetch_page', return_value=[]):
            run = run_probe('example', config, self.root / 'CloudStorage/experiments')
        self.assertEqual(sha256(self.base), self.before)
        self.assertTrue(pd.read_parquet(run / 'example/reviews_raw.parquet').empty)

    def test_invalid_rating_does_not_replace_base(self):
        with self.assertRaisesRegex(ValueError, 'inválidas'):
            self.collect([entry(score=6)])
        self.assertEqual(sha256(self.base), self.before)


if __name__ == '__main__':
    unittest.main()
