"""Continuity checks for the bounded Inter web coverage probe."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
from urllib.error import HTTPError
from io import BytesIO

from scripts.probe.app_store_web_coverage import inspect_page, retry_after_seconds, load_confirmed_state, run_coverage


def make_page(offset=0, year=2026, next_offset=10):
    rows = []
    for index in range(10):
        rows.append({"id": str(offset + index), "type": "user-reviews", "attributes": {
            "date": f"{year}-06-01T{20 - index:02d}:00:00Z", "review": "texto",
        }})
    next_link = None if next_offset is None else (
        f"/v1/catalog/br/apps/839711154/reviews?l=pt-BR&offset={next_offset}"
    )
    return {"data": rows, "next": next_link}


class CoveragePageTests(unittest.TestCase):
    def test_valid_page_advances_by_ten(self):
        result = inspect_page(make_page(), offset=0, seen_ids=set(), previous_oldest=None)
        self.assertEqual(result["review_count"], 10)
        self.assertEqual(result["first_id"], "0")
        self.assertEqual(result["last_id"], "9")
        self.assertEqual(result["next_offset"], 10)
        self.assertEqual(result["anomalies"], [])

    def test_duplicate_and_offset_jump_are_uncertain(self):
        result = inspect_page(make_page(next_offset=20), offset=0,
                              seen_ids={"3"}, previous_oldest=None)
        self.assertEqual(result["duplicate_ids"], ["3"])
        self.assertIn("duplicate_ids", result["anomalies"])
        self.assertIn("next_offset_jump", result["anomalies"])

    def test_date_increase_across_pages_is_uncertain(self):
        result = inspect_page(make_page(year=2026), offset=10, seen_ids=set(),
                              previous_oldest="2025-12-31T00:00:00Z")
        self.assertIn("date_increase_across_pages", result["anomalies"])

    def test_pre_2025_date_reaches_cutoff(self):
        result = inspect_page(make_page(year=2024, next_offset=10), offset=0,
                              seen_ids=set(), previous_oldest=None)
        self.assertTrue(result["before_cutoff"])

    def test_empty_page_with_next_is_uncertain(self):
        result = inspect_page({"data": [], "next": "/v1/catalog/br/apps/839711154/reviews?l=pt-BR&offset=10"},
                              offset=0, seen_ids=set(), previous_oldest=None)
        self.assertIn("empty_with_next", result["anomalies"])

    def test_retry_after_delta_and_http_date(self):
        now = datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("75", now), 75)
        self.assertEqual(retry_after_seconds("Mon, 05 Oct 2026 23:02:00 GMT", now), 120)
        self.assertIsNone(retry_after_seconds("broken", now))

    def test_reconstructs_last_confirmed_next_not_failed_attempt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pages").mkdir()
            body = json.dumps(make_page()).encode()
            (root / "pages/0001.json").write_bytes(body)
            from scripts.collect.storage import sha256
            good = {"page": 1, "offset": 0, "url": "https://apps.apple.com/api/apps/v1/catalog/br/apps/839711154/reviews?platform=web&l=pt-BR&sort=recent",
                    "http_status": 200, "body_file": "pages/0001.json", "body_sha256": sha256(root / "pages/0001.json"),
                    **inspect_page(make_page(), 0, set(), None)}
            failed = {"page": 2, "offset": 10, "http_status": 429}
            (root / "pages.jsonl").write_text(json.dumps(good) + "\n" + json.dumps(failed) + "\n")
            state = load_confirmed_state(root)
            self.assertEqual(state["next_offset"], 10)
            self.assertEqual(state["pages_http_200"], 1)
            self.assertEqual(state["pages_attempted"], 2)
            self.assertEqual(len(state["seen_ids"]), 10)

    def test_429_retries_same_offset_and_does_not_duplicate_page(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, *_): return self.body
        with TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "canonical.parquet"
            canonical.write_bytes(b"unchanged")
            output = root / "archive"
            output.mkdir()
            staging = root / "staging"
            called = []
            def fetch(request, timeout):
                called.append(request.full_url)
                if len(called) == 1:
                    raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "1"}, BytesIO(b"limited"))
                return FakeResponse(json.dumps(make_page(next_offset=None)).encode())
            with patch("scripts.probe.app_store_web_coverage.CANONICAL", canonical), \
                 patch("scripts.probe.app_store_web_coverage.STAGING_ROOT", staging), \
                 patch("scripts.probe.app_store_web_coverage.assert_local"), \
                 patch("scripts.probe.app_store_web_coverage.urlopen", side_effect=fetch), \
                 patch("scripts.probe.app_store_web_coverage.time.sleep") as sleep:
                result = run_coverage(delay_seconds=4, max_pages=1, output_parent=output)
            entries = [json.loads(x) for x in (result / "pages.jsonl").read_text().splitlines()]
            self.assertEqual([x["http_status"] for x in entries], [429, 200])
            self.assertEqual([x["offset"] for x in entries], [0, 0])
            self.assertEqual(len(called), 2)
            self.assertEqual(sleep.call_args.args[0], 4)
            self.assertEqual(json.loads((result / "summary.json").read_text())["unique_ids"], 10)

    def test_pause_then_resume_from_confirmed_next(self):
        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, *_): return self.body
        with TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "canonical.parquet"
            canonical.write_bytes(b"unchanged")
            output = root / "archive"
            output.mkdir()
            staging = root / "staging"
            called = []
            def fetch(request, timeout):
                called.append(request.full_url)
                if len(called) == 1:
                    return FakeResponse(json.dumps(make_page()).encode())
                if len(called) == 2:
                    raise HTTPError(request.full_url, 429, "Too Many Requests", {}, BytesIO(b"limited"))
                return FakeResponse(json.dumps(make_page(offset=10, year=2025, next_offset=None)).encode())
            with patch("scripts.probe.app_store_web_coverage.CANONICAL", canonical), \
                 patch("scripts.probe.app_store_web_coverage.STAGING_ROOT", staging), \
                 patch("scripts.probe.app_store_web_coverage.assert_local"), \
                 patch("scripts.probe.app_store_web_coverage.urlopen", side_effect=fetch), \
                 patch("scripts.probe.app_store_web_coverage.time.sleep"):
                paused = run_coverage(delay_seconds=4, max_pages=2, output_parent=output,
                                      max_429_retries=0)
                self.assertEqual(json.loads((paused / "summary.json").read_text())["stop_reason"],
                                 "rate_limited_paused")
                result = run_coverage(delay_seconds=4, max_pages=1, output_parent=output,
                                      resume_from=paused)
            entries = [json.loads(x) for x in (result / "pages.jsonl").read_text().splitlines()]
            self.assertEqual([x["offset"] for x in entries], [0, 10, 10])
            self.assertEqual([x["http_status"] for x in entries], [200, 429, 200])
            self.assertEqual(entries[1]["suggested_backoff_seconds"], 60)
            self.assertEqual(entries[1]["backoff_seconds"], 0)
            self.assertEqual(entries[1]["backoff_source"], "exponential")
            self.assertEqual(json.loads((result / "summary.json").read_text())["unique_ids"], 20)


if __name__ == "__main__":
    unittest.main()
