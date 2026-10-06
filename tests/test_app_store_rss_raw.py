"""Future RSS publications retain Apple's explicit updated offset."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import date

import pandas as pd

from scripts.collect.app_store import parse_review, publish


class RSSRawTimestampTests(unittest.TestCase):
    def test_publication_keeps_raw_updated_and_utc(self):
        raw = "2025-01-01T04:00:00-07:00"
        entry = {"id": {"label": "123"}, "updated": {"label": raw},
                 "im:rating": {"label": "5"}, "title": {"label": "title"},
                 "content": {"label": "body"}}
        review = parse_review(entry, "app", "br", "mostrecent")
        with TemporaryDirectory() as directory:
            output = Path(directory) / "rss.parquet"
            publish([review], output, date(2025, 1, 1))
            frame = pd.read_parquet(output)
        self.assertEqual(frame.loc[0, "rss_updated_raw"], raw)
        self.assertEqual(frame.loc[0, "data_avaliacao"], pd.Timestamp("2025-01-01T11:00:00Z"))


if __name__ == "__main__":
    unittest.main()
