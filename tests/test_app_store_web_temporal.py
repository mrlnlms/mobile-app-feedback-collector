"""Source-specific time comparison checks for the Inter experiment."""

import unittest

from scripts.probe.app_store_web_temporal import compare_source_times, parse_utc


class SourceTimeTests(unittest.TestCase):
    def test_preserves_raw_and_converts_each_source_to_utc(self):
        pair = compare_source_times("2026-02-27T11:41:00-07:00", "2026-02-27T19:41:00Z")
        self.assertEqual(pair["rss_updated_raw"], "2026-02-27T11:41:00-07:00")
        self.assertEqual(pair["rss_updated_utc"], "2026-02-27T18:41:00+00:00")
        self.assertEqual(pair["web_date_raw"], "2026-02-27T19:41:00Z")
        self.assertEqual(pair["web_date_utc"], "2026-02-27T19:41:00+00:00")
        self.assertEqual(pair["rss_minus_web_seconds"], -3600)

    def test_larger_event_difference_remains_visible(self):
        pair = compare_source_times("2026-02-28T11:41:00-07:00", "2026-02-27T19:41:00Z")
        self.assertEqual(pair["rss_minus_web_seconds"], 82800)

    def test_rejects_timestamp_without_timezone(self):
        with self.assertRaisesRegex(ValueError, "sem offset"):
            parse_utc("2026-02-27T11:41:00")


if __name__ == "__main__":
    unittest.main()
