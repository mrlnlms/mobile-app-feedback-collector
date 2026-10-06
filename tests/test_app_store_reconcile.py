"""Identity, temporal provenance, and source separation in RSS + Web reconciliation."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from scripts.collect.app_store_reconcile import build_reconciled
from scripts.collect.storage import sha256


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.rss_path = root / "rss.parquet"
        self.web_path = root / "web.parquet"
        self.capture = root / "capture"
        (self.capture / "pages").mkdir(parents=True)
        pd.DataFrame([
            {"id_review": "1", "usuario": "rss user", "nota": 5,
             "data_avaliacao": pd.Timestamp("2025-01-01T11:00:00Z"), "titulo": "rss title",
             "texto_avaliacao": "rss body", "versao_app": "1", "sorts_encontrados": "mostrecent",
             "apple_app_id": "123", "pais": "br"},
            {"id_review": "2", "usuario": "rss only", "nota": 3,
             "data_avaliacao": pd.Timestamp("2025-01-02T11:00:00Z"), "titulo": "only",
             "texto_avaliacao": "only", "versao_app": "1", "sorts_encontrados": "mosthelpful",
             "apple_app_id": "123", "pais": "br"},
        ]).to_parquet(self.rss_path, index=False)
        pd.DataFrame([
            {"id_review": "1", "web_date_raw": "2025-01-01T12:00:00Z",
             "web_date_utc": pd.Timestamp("2025-01-01T12:00:00Z"), "web_usuario": "web user",
             "web_nota": 4, "web_titulo": "web title", "web_texto_avaliacao": "web body",
             "web_is_edited": True, "web_offset": 0, "web_run": "run",
             "web_body_file": "pages/1.json", "web_body_sha256": "abc", "apple_app_id": "123", "pais": "br"},
            {"id_review": "3", "web_date_raw": "2025-01-03T12:00:00Z",
             "web_date_utc": pd.Timestamp("2025-01-03T12:00:00Z"), "web_usuario": "web only",
             "web_nota": 5, "web_titulo": "only", "web_texto_avaliacao": "only",
             "web_is_edited": False, "web_offset": 10, "web_run": "run",
             "web_body_file": "pages/2.json", "web_body_sha256": "def", "apple_app_id": "123", "pais": "br"},
        ]).to_parquet(self.web_path, index=False)
        body = {"feed": {"entry": [{"id": {"label": "1"},
                "updated": {"label": "2025-01-01T04:00:00-07:00"}, "im:rating": {"label": "5"}}]}}
        path = self.capture / "pages/1.json"
        path.write_text(json.dumps(body))
        (self.capture / "requests.json").write_text(json.dumps([
            {"http_status": 200, "body_file": "pages/1.json", "body_sha256": sha256(path)}]))

    def tearDown(self):
        self.temp.cleanup()

    def test_full_outer_join_preserves_both_versions_and_missing_raw(self):
        result = build_reconciled(self.rss_path, self.web_path, [self.capture]).set_index("id_review")
        self.assertEqual(result.source_presence.to_dict(), {"1": "rss_web", "2": "rss_only", "3": "web_only"})
        self.assertEqual(result.loc["1", "rss_updated_raw"], "2025-01-01T04:00:00-07:00")
        self.assertEqual(result.loc["1", "rss_web_time_delta_seconds"], -3600)
        self.assertEqual(result.loc["1", "rss_texto_avaliacao"], "rss body")
        self.assertEqual(result.loc["1", "web_texto_avaliacao"], "web body")
        self.assertFalse(result.loc["1", "rss_web_text_equal"])
        self.assertFalse(result.loc["2", "rss_updated_raw_available"])
        self.assertTrue(pd.isna(result.loc["2", "rss_updated_raw"]))
        self.assertEqual(result.loc["2", "rss_updated_utc"], pd.Timestamp("2025-01-02T11:00:00Z"))
        self.assertNotIn("review_date", result.columns)

    def test_tampered_rss_capture_is_rejected(self):
        (self.capture / "pages/1.json").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "hash divergente"):
            build_reconciled(self.rss_path, self.web_path, [self.capture])

    def test_future_rss_base_raw_is_used_without_a_separate_capture(self):
        rss = pd.read_parquet(self.rss_path)
        rss["rss_updated_raw"] = ["2025-01-01T04:00:00-07:00", None]
        rss.to_parquet(self.rss_path, index=False)
        result = build_reconciled(self.rss_path, self.web_path).set_index("id_review")
        self.assertEqual(result.loc["1", "rss_updated_raw_origin"], "rss_base")
        self.assertEqual(result.loc["1", "rss_updated_raw"], "2025-01-01T04:00:00-07:00")
        self.assertFalse(result.loc["2", "rss_updated_raw_available"])


if __name__ == "__main__":
    unittest.main()
