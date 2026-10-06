"""Source-set and timestamp checks for the Inter Web/RSS experiment."""

import unittest

from scripts.probe.app_store_web_analysis import compare_id_sets, parse_rss_reviews, temporal_pair


class WebAnalysisTests(unittest.TestCase):
    def test_parse_raw_rss_updated_value(self):
        payload = {"feed": {"entry": [{"id": {"label": "42"},
                                        "updated": {"label": "2025-01-03T07:37:54-07:00"},
                                        "im:rating": {"label": "4"}}]}}
        result = parse_rss_reviews(payload)
        self.assertEqual(result["42"], "2025-01-03T07:37:54-07:00")

    def test_winter_fixed_minus_seven_explains_one_hour(self):
        result = temporal_pair("2025-01-03T07:37:54-07:00",
                               "2025-01-03T15:37:54Z",
                               "2025-01-03T14:37:54+00:00")
        self.assertEqual(result["rss_minus_web_seconds"], -3600)
        self.assertEqual(result["rss_offset_seconds"], -25200)
        self.assertEqual(result["los_angeles_offset_seconds"], -28800)
        self.assertTrue(result["same_local_wall_clock"])
        self.assertTrue(result["rss_parse_matches_parquet"])

    def test_summer_minus_seven_matches_web(self):
        result = temporal_pair("2025-09-06T07:31:37-07:00",
                               "2025-09-06T14:31:37Z",
                               "2025-09-06T14:31:37+00:00")
        self.assertEqual(result["rss_minus_web_seconds"], 0)
        self.assertEqual(result["los_angeles_offset_seconds"], -25200)
        self.assertTrue(result["same_local_wall_clock"])

    def test_source_sets_are_directional(self):
        result = compare_id_sets({"a", "b"}, {"b", "c"})
        self.assertEqual(result["web_and_rss"], {"b"})
        self.assertEqual(result["web_only"], {"a"})
        self.assertEqual(result["rss_only"], {"c"})


if __name__ == "__main__":
    unittest.main()
