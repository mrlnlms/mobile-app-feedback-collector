"""Focused tests for the reusable App Store Web collector."""

import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.collect.app_store_web import (build_web_frame, collect_web, first_url,
                                           materialize_web, replay_run)
from scripts.collect.storage import sha256


def page(offset=0, next_offset=None):
    day = "10" if offset == 0 else "09"
    return {"data": [{"id": str(offset + i), "type": "user-reviews", "attributes": {
        "date": f"2025-01-{day}T{20-i:02d}:00:00Z", "review": f"review {i}",
        "title": "title", "rating": 5, "userName": "user", "isEdited": False,
    }} for i in range(10)],
        "next": f"/v1/catalog/br/apps/123/reviews?l=pt-BR&offset={next_offset}" if next_offset is not None else None}


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, *_): return self.payload


class WebCollectorTests(unittest.TestCase):
    def test_429_retries_same_offset_and_preserves_raw_body(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fetch(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    raise HTTPError(request.full_url, 429, "limited", {"Retry-After": "8"}, BytesIO(b"limited"))
                return Response(page())
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=fetch), \
                 patch("scripts.collect.app_store_web.time.sleep") as sleep:
                run = collect_web("test", "123", "br", delay_seconds=4, max_pages=1)
            self.assertEqual(calls, [first_url("123", "br")] * 2)
            entries = [json.loads(line) for line in (run / "pages.jsonl").read_text().splitlines()]
            self.assertEqual([entry["http_status"] for entry in entries], [429, 200])
            self.assertEqual([entry["offset"] for entry in entries], [0, 0])
            self.assertEqual(entries[0]["backoff_seconds"], 8)
            sleep.assert_called_once_with(8)
            for entry in entries:
                self.assertEqual(sha256(run / entry["body_file"]), entry["body_sha256"])
            self.assertEqual(len(build_web_frame(run, "123", "br")), 10)

    def test_pause_resume_and_materialize_without_refetch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fetch(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    return Response(page(next_offset=10))
                if len(calls) == 2:
                    raise HTTPError(request.full_url, 429, "limited", {}, BytesIO(b"limited"))
                return Response(page(offset=10))
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"), \
                 patch("scripts.collect.app_store_web.SNAPSHOT_ROOT", root / "snapshots"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=fetch), \
                 patch("scripts.collect.app_store_web.time.sleep"):
                paused = collect_web("test", "123", "br", delay_seconds=4, max_pages=2, max_429_retries=0)
                self.assertEqual(json.loads((paused / "summary.json").read_text())["stop_reason"], "rate_limited_paused")
                resumed = collect_web("test", "123", "br", delay_seconds=4, max_pages=1, resume_from=paused)
                self.assertEqual([e["offset"] for e in replay_run(resumed, "123", "br")["confirmed"]], [0, 10])
                receipt = materialize_web("test", "123", "br", resumed)
            self.assertEqual(len(calls), 3)
            self.assertEqual(receipt["reviews"], 20)
            self.assertEqual(sha256(root / "raw/test/reviews_web.parquet"), receipt["sha256"])

    def test_replay_rejects_tampered_body(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", return_value=Response(page())):
                run = collect_web("test", "123", "br", delay_seconds=4, max_pages=1)
            entry = json.loads((run / "pages.jsonl").read_text().splitlines()[0])
            (run / entry["body_file"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "hash divergente"):
                build_web_frame(run, "123", "br")

    def test_later_run_preserves_ids_absent_from_new_web_sample(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            second = page()
            for index, row in enumerate(second["data"]):
                row["id"] = str(20 + index)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"), \
                 patch("scripts.collect.app_store_web.SNAPSHOT_ROOT", root / "snapshots"):
                with patch("scripts.collect.app_store_web.urlopen", return_value=Response(page())):
                    first = collect_web("test", "123", "br", delay_seconds=4, max_pages=1)
                materialize_web("test", "123", "br", first)
                with patch("scripts.collect.app_store_web.urlopen", return_value=Response(second)):
                    later = collect_web("test", "123", "br", delay_seconds=4, max_pages=1)
                receipt = materialize_web("test", "123", "br", later)
                repeat = materialize_web("test", "123", "br", later)
            self.assertEqual(receipt["reviews"], 20)
            self.assertEqual(receipt["new_ids"], 10)
            self.assertEqual(receipt["older_ids_preserved"], 10)
            self.assertTrue(Path(receipt["snapshot"]).is_file())
            self.assertTrue(repeat["unchanged"])
            self.assertEqual(repeat["sha256"], receipt["sha256"])

    def test_partial_run_cannot_replace_web_archive(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"), \
                 patch("scripts.collect.app_store_web.urlopen", return_value=Response(page(next_offset=10))):
                run = collect_web("test", "123", "br", delay_seconds=4, max_pages=1)
                with self.assertRaisesRegex(ValueError, "fronteira"):
                    materialize_web("test", "123", "br", run)

    def test_429_on_first_offset_can_be_archived_and_resumed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fetch(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    raise HTTPError(request.full_url, 429, "limited", {}, BytesIO(b"limited"))
                return Response(page())
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=fetch):
                paused = collect_web("test", "123", "br", delay_seconds=4, max_pages=1, max_429_retries=0)
                self.assertEqual(replay_run(paused, "123", "br")["ids"], set())
                resumed = collect_web("test", "123", "br", delay_seconds=4, max_pages=1, resume_from=paused)
            self.assertEqual(calls, [first_url("123", "br")] * 2)
            self.assertEqual(len(replay_run(resumed, "123", "br")["ids"]), 10)


if __name__ == "__main__":
    unittest.main()
