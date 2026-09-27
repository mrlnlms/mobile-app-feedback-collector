"""Testes locais dos caminhos e da preservação das bases, sem acessar lojas."""

import hashlib
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.collect.app_store import COLS, publish
from scripts.collect.google_play import write_final_base
from scripts.probe.app_store import run_probe


def review(review_id, timestamp):
    return {
        "id_review": review_id,
        "data_avaliacao": timestamp,
        "nota": 5,
        "texto_avaliacao": "Exemplo local",
        "titulo": "Teste",
        "sorts_encontrados": "mostrecent",
    }


def app_frame(*records):
    frame = pd.DataFrame(records, columns=COLS)
    frame["data_avaliacao"] = pd.to_datetime(frame["data_avaliacao"], utc=True)
    return frame


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StorageTests(unittest.TestCase):
    def test_google_play_keeps_previous_base_in_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "data/raw/google_play/nubank/reviews_raw.parquet"
            snapshots = root / "data/runs/google_play/snapshots/nubank"
            raw.parent.mkdir(parents=True)
            pd.DataFrame([review("old", "2026-09-01")]).to_parquet(raw, index=False)
            old_hash = digest(raw)

            write_final_base(
                pd.DataFrame([review("new", "2026-09-02")]),
                raw,
                datetime(2025, 1, 1),
                snapshots,
            )

            self.assertEqual(set(pd.read_parquet(raw)["id_review"]), {"old", "new"})
            self.assertEqual(len(list(snapshots.glob("*.parquet"))), 1)
            self.assertEqual(digest(next(snapshots.glob("*.parquet"))), old_hash)
            self.assertFalse((raw.parent / "snapshots").exists())

    def test_app_store_keeps_previous_base_in_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "data/raw/app_store/inter/reviews_raw.parquet"
            snapshots = root / "data/runs/app_store/snapshots/inter"
            raw.parent.mkdir(parents=True)
            app_frame(review("old", "2026-09-01T00:00:00Z")).to_parquet(raw, index=False)
            old_hash = digest(raw)

            publish([review("new", "2026-09-02T00:00:00Z")], raw, date(2025, 1, 1), snapshots)

            self.assertEqual(set(pd.read_parquet(raw)["id_review"]), {"old", "new"})
            self.assertEqual(len(list(snapshots.glob("*.parquet"))), 1)
            self.assertEqual(digest(next(snapshots.glob("*.parquet"))), old_hash)
            self.assertFalse((raw.parent / "snapshots").exists())

    def test_app_store_probe_does_not_change_canonical_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_dir = root / "data/raw/app_store"
            original = raw_dir / "caixa/reviews_raw.parquet"
            original.parent.mkdir(parents=True)
            app_frame(review("old", "2026-09-01T00:00:00Z")).to_parquet(original, index=False)
            old_hash = digest(original)
            config = {
                "banks": {"caixa": {"apple_app_id": "490813624", "name": "Caixa"}},
                "app_store": {
                    "country": "br", "sorts": ["mostrecent"], "max_pages_per_sort": 1,
                    "delay_seconds": 0, "max_retries": 1,
                    "output_dir": str(raw_dir),
                    "checkpoint_dir": str(root / "data/runs/app_store/checkpoints"),
                    "report_dir": str(root / "data/runs/app_store/reports"),
                    "snapshot_dir": str(root / "data/runs/app_store/snapshots"),
                },
            }

            def fake_collect(bank_key, bank, settings, allow_empty):
                self.assertTrue(allow_empty)
                self.assertNotEqual(Path(settings["output_dir"]), raw_dir)
                repeated = Path(settings["output_dir"]) / bank_key / "reviews_raw.parquet"
                repeated.parent.mkdir(parents=True)
                app_frame(review("new", "2026-09-02T00:00:00Z")).to_parquet(repeated, index=False)
                return {"paginas_por_sort": {"mostrecent": 1}}

            with patch("scripts.probe.app_store.collect_bank", side_effect=fake_collect):
                run_root = run_probe("caixa", config, root / "data/runs/app_store/experiments")

            self.assertEqual(digest(original), old_hash)
            self.assertTrue((run_root / "probe.json").is_file())
            self.assertTrue((run_root / "caixa/reviews_raw.parquet").is_file())
            self.assertFalse((root / "data/runs/app_store/snapshots/caixa").exists())


if __name__ == "__main__":
    unittest.main()
