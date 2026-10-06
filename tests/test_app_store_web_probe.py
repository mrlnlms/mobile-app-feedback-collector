"""Focused checks for the isolated App Store web probe."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.probe.app_store_web import compare_runs, extract_reviews, normalize_next, repeated_page


class WebProbeTests(unittest.TestCase):
    def test_next_keeps_storefront_and_restores_web_platform(self):
        url = normalize_next(
            "/v1/catalog/br/apps/839711154/reviews?l=pt-BR&offset=10"
        )
        self.assertEqual(
            url,
            "https://apps.apple.com/api/apps/v1/catalog/br/apps/839711154/reviews"
            "?l=pt-BR&offset=10&platform=web",
        )

    def test_recent_next_keeps_sort_across_pages(self):
        url = normalize_next(
            "/v1/catalog/br/apps/839711154/reviews?l=pt-BR&offset=10",
            sort="recent",
        )
        self.assertEqual(
            url,
            "https://apps.apple.com/api/apps/v1/catalog/br/apps/839711154/reviews"
            "?l=pt-BR&offset=10&platform=web&sort=recent",
        )

    def test_next_rejects_other_app_and_host(self):
        for link in (
            "/v1/catalog/br/apps/123/reviews?offset=10",
            "https://example.com/v1/catalog/br/apps/839711154/reviews?offset=10",
        ):
            with self.subTest(link=link), self.assertRaises(ValueError):
                normalize_next(link)

    def test_extract_requires_id_text_and_timezone_date(self):
        good = {"data": [{"id": "42", "type": "user-reviews", "attributes": {
            "date": "2025-03-04T10:11:12Z", "review": "texto", "title": "título",
            "rating": 4, "userName": "alguém"}}]}
        self.assertEqual(extract_reviews(good)[0]["id_review"], "42")
        for field in ("id", "review", "date"):
            broken = {"data": [{"id": "42", "type": "user-reviews", "attributes": {
                "date": "2025-03-04T10:11:12Z", "review": "texto"}}]}
            if field == "id":
                del broken["data"][0]["id"]
            else:
                del broken["data"][0]["attributes"][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                extract_reviews(broken)

    def test_repeated_page_requires_all_ids_to_match(self):
        self.assertTrue(repeated_page(["a", "b"], ["a", "b"]))
        self.assertFalse(repeated_page(["a", "b"], ["b", "c"]))

    def test_compare_unequal_runs_uses_matching_page_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            first.mkdir()
            second.mkdir()
            for root, ids, pages in (
                (first, ["a", "b"], [["a"], ["b"]]),
                (second, ["a"], [["a"]]),
            ):
                (root / "reviews.json").write_text(json.dumps({i: {"text": i} for i in ids}))
                (root / "requests.json").write_text(json.dumps(
                    [{"http_status": 200, "ids": page} for page in pages]
                ))
                (root / "summary.json").write_text(json.dumps(
                    {"started_at": "2026-10-05T00:00:00Z", "stop_reason": "page_limit"}
                ))
            result = compare_runs(first, second)
            self.assertEqual(result["first_only_ids"], ["b"])
            self.assertEqual(result["comparable_pages"], 1)
            self.assertEqual(result["first_only_ids_on_comparable_pages"], [])
            self.assertEqual(result["pages_with_different_ids_or_order"], [])
            (second / "summary.json").write_text(json.dumps({
                "started_at": "2026-10-05T00:00:00Z", "stop_reason": "page_limit",
                "sort": "recent", "start_offset": 0,
            }))
            with self.assertRaises(ValueError):
                compare_runs(first, second)

    def test_compare_detects_changed_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ("first", "second")]
            for root, next_link, digest in zip(roots, ("?offset=10", "?offset=20"), ("a", "b")):
                (root / "pages").mkdir(parents=True)
                (root / "pages" / "0001.json").write_text(json.dumps({"data": [], "next": next_link}))
                (root / "reviews.json").write_text(json.dumps({"42": {"text": "same"}}))
                (root / "requests.json").write_text(json.dumps([{
                    "http_status": 200, "ids": ["42"], "body_file": "pages/0001.json",
                    "body_sha256": digest,
                }]))
                (root / "summary.json").write_text(json.dumps({
                    "started_at": "2026-10-05T00:00:00Z", "stop_reason": "page_limit",
                }))
            result = compare_runs(*roots)
            self.assertEqual(result["pages_with_different_ids_or_order"], [])
            self.assertEqual(result["changed_shared_ids"], [])
            self.assertEqual(result["pages_with_different_next"], [1])
            self.assertEqual(result["pages_with_different_raw_body"], [1])


if __name__ == "__main__":
    unittest.main()
