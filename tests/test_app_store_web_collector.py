"""Focused tests for the reusable App Store Web collector."""

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch
from urllib.error import HTTPError

from scripts.collect.app_store_web import (build_web_frame, collect_web, first_url,
                                           materialize_web, next_url, recover_continuity,
                                           recover_source_end,
                                           replay_run)
from scripts.collect.storage import sha256


def page(offset=0, next_offset=None):
    day = "10" if offset == 0 else "09"
    return {"data": [{"id": str(offset + i), "type": "user-reviews", "attributes": {
        "date": f"2025-01-{day}T{20-i:02d}:00:00Z", "review": f"review {i}",
        "title": "title", "rating": 5, "userName": "user", "isEdited": False,
    }} for i in range(10)],
        "next": f"/v1/catalog/br/apps/123/reviews?l=pt-BR&offset={next_offset}" if next_offset is not None else None}


def timeline_page(ids, next_offset=None):
    rows = []
    for review_id in ids:
        timestamp = datetime(2025, 1, 10, tzinfo=timezone.utc) - timedelta(minutes=int(review_id))
        rows.append({"id": str(review_id), "type": "user-reviews", "attributes": {
            "date": timestamp.isoformat().replace("+00:00", "Z"),
            "review": f"review {review_id}", "title": "title", "rating": 5,
            "userName": "user", "isEdited": False,
        }})
    return {"data": rows, "next": (
        f"/v1/catalog/br/apps/123/reviews?l=pt-BR&offset={next_offset}"
        if next_offset is not None else None)}


def recovery_fixture(root, source, missing_tail=False, changed_review=False,
                     initial_rate_limit=False):
    """Preserve a synthetic probe over an apparent source end."""
    probe_root = root / "probes"
    probe_root.mkdir()
    page20 = timeline_page(range(5, 15), 30)
    page30_ids = [15, 16, 17, 18, 20, 21, 22, 23, 24, 25] if missing_tail else list(range(15, 25))
    page30 = timeline_page(page30_ids, 40)
    page40 = timeline_page(range(26, 36) if missing_tail else range(25, 35), 50)
    if changed_review:
        page20["data"][0]["attributes"]["review"] = "changed review"
    pages = []
    rate_limits = []
    responses = [
        (20, 200, page20), (30, 429, None), (30, 200, page30), (40, 200, page40),
    ]
    if initial_rate_limit:
        responses.insert(0, (20, 429, None))
    for sequence, (offset, status, payload) in enumerate(responses):
        folder = probe_root / f"page_{sequence}"
        folder.mkdir()
        body = json.dumps(payload).encode() if status == 200 else b"limited"
        (folder / "response.body").write_bytes(body)
        requested_at = (datetime(2025, 1, 11, tzinfo=timezone.utc)
                        + timedelta(seconds=sequence * 30)).isoformat()
        metadata = {"url": next_url(f"/v1/catalog/br/apps/123/reviews?l=pt-BR&offset={offset}", "123", "br"),
                    "requested_at": requested_at, "received_at": requested_at,
                    "http_status": status, "body_file": "response.body",
                    "body_sha256": sha256(folder / "response.body")}
        (folder / "probe.json").write_text(json.dumps(metadata))
        if status == 200:
            pages.append({"offset": offset, "probe_dir": str(folder),
                          "body_file": str(folder / "response.body"),
                          "body_sha256": metadata["body_sha256"]})
        else:
            rate_limits.append({"probe_dir": str(folder), "body_sha256": metadata["body_sha256"]})
    canonical = root / "raw/test/reviews_web.parquet"
    checkpoint = {"kind": "app_store_web_source_end_recovery_probe", "version": 1,
                  "bank": "test", "app_id": "123", "storefront": "br",
                  "source_run": str(source), "source_journal_sha256": sha256(source / "pages.jsonl"),
                  "canonical_web_parquet": str(canonical), "canonical_web_sha256": sha256(canonical),
                  "source_last_offset": 10, "direct_probe_first_offset": 20,
                  "last_probe_offset": 40, "next_offset": 50,
                  "next_url": next_url("/v1/catalog/br/apps/123/reviews?l=pt-BR&offset=50", "123", "br"),
                  "new_review_ids": [str(value) for value in (range(20, 25) if not missing_tail else range(20, 26))]
                  + [str(value) for value in (range(25, 35) if not missing_tail else range(26, 36))],
                  "probe_pages": pages, "rate_limit_responses": rate_limits}
    path = root / "checkpoint.json"
    path.write_text(json.dumps(checkpoint))
    return path


class Response:
    status = 200
    headers = {"Content-Type": "application/json"}
    def __init__(self, payload): self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, *_): return self.payload


class WebCollectorTests(unittest.TestCase):
    def test_linked_review_404_retries_same_offset_and_keeps_body(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            error_body = b'{"errors":[{"status":"404","code":"40403","detail":"No related resources found for reviews"}]}'
            def fetch(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    return Response(page(next_offset=10))
                if len(calls) == 2:
                    raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(error_body))
                return Response(page(offset=10, next_offset=20))
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=fetch), \
                 patch("scripts.collect.app_store_web.time.sleep") as sleep:
                run = collect_web("test", "123", "br", delay_seconds=4, max_pages=2)
            entries = [json.loads(line) for line in (run / "pages.jsonl").read_text().splitlines()]
            self.assertEqual([entry["http_status"] for entry in entries], [200, 404, 200])
            self.assertEqual(calls[1:], [calls[1]] * 2)
            self.assertEqual(entries[1]["review_404_retry_number"], 1)
            self.assertTrue(entries[1]["will_retry"])
            self.assertEqual(sha256(run / entries[1]["body_file"]), entries[1]["body_sha256"])
            self.assertIn(call(60), sleep.call_args_list)
            self.assertEqual(len(replay_run(run, "123", "br")["ids"]), 20)
            self.assertEqual(json.loads((run / "summary.json").read_text())["review_404_attempts_this_run"], 1)

    def test_persistent_review_404_pauses_at_confirmed_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            error_body = b'{"errors":[{"status":"404","code":"40403","detail":"No related resources found for reviews"}]}'
            calls = []
            def fetch(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    return Response(page(next_offset=10))
                raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(error_body))
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=fetch), \
                 patch("scripts.collect.app_store_web.time.sleep"):
                run = collect_web("test", "123", "br", delay_seconds=4, max_pages=2,
                                  max_404_retries=1)
            summary = json.loads((run / "summary.json").read_text())
            self.assertEqual(summary["stop_reason"], "http_404")
            self.assertEqual(summary["next_offset"], 10)
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(replay_run(run, "123", "br")["ids"]), 10)
            with self.assertRaisesRegex(ValueError, "fronteira"):
                materialize_web("test", "123", "br", run)

    def test_unrelated_404_is_not_retried(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fetch(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1:
                    return Response(page(next_offset=10))
                raise HTTPError(request.full_url, 404, "Not Found", {}, BytesIO(b'{"errors":[{"code":"40401"}]}'))
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=fetch), \
                 patch("scripts.collect.app_store_web.time.sleep"):
                run = collect_web("test", "123", "br", delay_seconds=4, max_pages=2)
            summary = json.loads((run / "summary.json").read_text())
            self.assertEqual(summary["stop_reason"], "http_404")
            self.assertEqual(summary["review_404_attempts_this_run"], 0)
            self.assertEqual(len(calls), 2)

    def make_source_end(self, root):
        responses = iter([Response(timeline_page(range(10), 10)),
                          Response(timeline_page(range(10, 20)))])
        with patch("scripts.collect.app_store_web.urlopen", side_effect=lambda request, timeout: next(responses)), \
             patch("scripts.collect.app_store_web.time.sleep"):
            source = collect_web("test", "123", "br", cutoff=date(2025, 1, 1),
                                 delay_seconds=4, max_pages=2)
        self.assertEqual(json.loads((source / "summary.json").read_text())["stop_reason"], "source_end")
        materialize_web("test", "123", "br", source)
        return source

    def make_source_continuity_pause(self, root, anomalous=True):
        pages = [Response(timeline_page(range(10), 10)),
                 Response(timeline_page(range(10, 20), 20))]
        if anomalous:
            pages.append(Response(timeline_page(range(5, 15), 30)))
        responses = iter(pages)
        with patch("scripts.collect.app_store_web.urlopen", side_effect=lambda request, timeout: next(responses)), \
             patch("scripts.collect.app_store_web.time.sleep"):
            source = collect_web("test", "123", "br", cutoff=date(2025, 1, 1),
                                 delay_seconds=4, max_pages=len(pages))
        expected_reason = "continuity_uncertain" if anomalous else "page_limit"
        self.assertEqual(json.loads((source / "summary.json").read_text())["stop_reason"], expected_reason)
        canonical = root / "raw/test/reviews_web.parquet"
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b"unchanged canonical archive")
        return source

    def test_recover_continuity_imports_repeated_span_and_resumes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"):
                source = self.make_source_continuity_pause(root)
                checkpoint = recovery_fixture(root, source)
                data = json.loads(checkpoint.read_text())
                data["kind"] = "app_store_web_continuity_recovery_probe"
                checkpoint.write_text(json.dumps(data))
                canonical = root / "raw/test/reviews_web.parquet"
                before = sha256(canonical)
                with patch("scripts.collect.app_store_web.urlopen", side_effect=AssertionError("No HTTP during import")):
                    recovered = recover_continuity("test", "123", "br", date(2025, 1, 1),
                                                   source, checkpoint)
                state = replay_run(recovered, "123", "br", date(2025, 1, 1))
                summary = json.loads((recovered / "summary.json").read_text())
                self.assertEqual(summary["stop_reason"], "page_limit")
                self.assertEqual(summary["recovery_mode"], "continuity")
                self.assertEqual(summary["pages_http_200"], 5)
                self.assertEqual(summary["unique_ids"], 35)
                self.assertEqual(summary["next_offset"], 50)
                self.assertEqual(len(state["entries"]), 7)
                self.assertEqual(len(build_web_frame(recovered, "123", "br")), 35)
                self.assertTrue(state["entries"][3]["recovery_after_anomaly"])
                self.assertEqual(sha256(canonical), before)
                with patch("scripts.collect.app_store_web.urlopen",
                           return_value=Response(timeline_page(range(35, 45), 60))):
                    continued = collect_web("test", "123", "br", cutoff=date(2025, 1, 1),
                                            delay_seconds=4, max_pages=1, resume_from=recovered)
                self.assertEqual(len(replay_run(continued, "123", "br")["ids"]), 45)

    def test_recover_continuity_rejects_run_without_pending_anomaly(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"):
                source = self.make_source_continuity_pause(root, anomalous=False)
                checkpoint = recovery_fixture(root, source)
                data = json.loads(checkpoint.read_text())
                data["kind"] = "app_store_web_continuity_recovery_probe"
                checkpoint.write_text(json.dumps(data))
                with patch("scripts.collect.app_store_web.urlopen", side_effect=AssertionError("No HTTP during import")):
                    with self.assertRaisesRegex(ValueError, "anomalia"):
                        recover_continuity("test", "123", "br", date(2025, 1, 1),
                                           source, checkpoint)

    def test_recover_continuity_accepts_rate_limit_before_first_recovery_page(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"):
                source = self.make_source_continuity_pause(root)
                checkpoint = recovery_fixture(root, source, initial_rate_limit=True)
                data = json.loads(checkpoint.read_text())
                data["kind"] = "app_store_web_continuity_recovery_probe"
                checkpoint.write_text(json.dumps(data))
                with patch("scripts.collect.app_store_web.urlopen",
                           side_effect=AssertionError("No HTTP during import")):
                    recovered = recover_continuity("test", "123", "br", date(2025, 1, 1),
                                                   source, checkpoint)
                state = replay_run(recovered, "123", "br", date(2025, 1, 1))
                self.assertEqual([entry["http_status"] for entry in state["entries"][3:6]],
                                 [429, 200, 429])
                self.assertEqual(len(state["ids"]), 35)
                self.assertEqual(json.loads((recovered / "summary.json").read_text())
                                 ["recovery_imported_429"], 2)

    def test_recover_continuity_rejects_initial_rate_limit_at_wrong_offset(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"):
                source = self.make_source_continuity_pause(root)
                checkpoint = recovery_fixture(root, source, initial_rate_limit=True)
                data = json.loads(checkpoint.read_text())
                data["kind"] = "app_store_web_continuity_recovery_probe"
                checkpoint.write_text(json.dumps(data))
                first_attempt = root / "probes/page_0/probe.json"
                metadata = json.loads(first_attempt.read_text())
                metadata["url"] = next_url(
                    "/v1/catalog/br/apps/123/reviews?l=pt-BR&offset=30", "123", "br")
                first_attempt.write_text(json.dumps(metadata))
                with self.assertRaisesRegex(ValueError, "fora da sequência"):
                    recover_continuity("test", "123", "br", date(2025, 1, 1),
                                       source, checkpoint)

    def test_recover_source_end_imports_probe_and_resumes_at_next_offset(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"), \
                 patch("scripts.collect.app_store_web.SNAPSHOT_ROOT", root / "snapshots"):
                source = self.make_source_end(root)
                checkpoint = recovery_fixture(root, source)
                canonical = root / "raw/test/reviews_web.parquet"
                before = sha256(canonical)
                with patch("scripts.collect.app_store_web.urlopen", side_effect=AssertionError("No HTTP during import")):
                    recovered = recover_source_end("test", "123", "br", date(2025, 1, 1),
                                                   source, checkpoint)
                state = replay_run(recovered, "123", "br", date(2025, 1, 1))
                summary = json.loads((recovered / "summary.json").read_text())
                self.assertEqual(summary["stop_reason"], "page_limit")
                self.assertEqual(summary["pages_http_200"], 5)
                self.assertEqual(summary["unique_ids"], 35)
                self.assertEqual(summary["next_offset"], 50)
                self.assertEqual(len(state["entries"]), 6)
                self.assertEqual(len(build_web_frame(recovered, "123", "br")), 35)
                self.assertEqual(sha256(canonical), before)
                with self.assertRaisesRegex(ValueError, "fronteira"):
                    materialize_web("test", "123", "br", recovered)
                calls = []
                def fetch(request, timeout):
                    calls.append(request.full_url)
                    return Response(timeline_page(range(35, 45), 60))
                with patch("scripts.collect.app_store_web.urlopen", side_effect=fetch):
                    continued = collect_web("test", "123", "br", cutoff=date(2025, 1, 1),
                                            delay_seconds=4, max_pages=1, resume_from=recovered)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0], state["next_url"])
                self.assertEqual(len(replay_run(continued, "123", "br")["ids"]), 45)
                self.assertEqual(sha256(canonical), before)

    def test_recovery_rejects_missing_tail_before_new_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"), \
                 patch("scripts.collect.app_store_web.SNAPSHOT_ROOT", root / "snapshots"):
                source = self.make_source_end(root)
                checkpoint = recovery_fixture(root, source, missing_tail=True)
                with patch("scripts.collect.app_store_web.urlopen", side_effect=AssertionError("No HTTP during import")):
                    with self.assertRaisesRegex(ValueError, "último ID|tail"):
                        recover_source_end("test", "123", "br", date(2025, 1, 1), source, checkpoint)

    def test_recovery_rejects_changed_archived_review_and_tampered_body(self):
        for changed in (True, False):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                     patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                     patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"), \
                     patch("scripts.collect.app_store_web.SNAPSHOT_ROOT", root / "snapshots"):
                    source = self.make_source_end(root)
                    checkpoint = recovery_fixture(root, source, changed_review=changed)
                    if not changed:
                        (root / "probes/page_0/response.body").write_bytes(b"tampered")
                    with patch("scripts.collect.app_store_web.urlopen", side_effect=AssertionError("No HTTP during import")):
                        with self.assertRaisesRegex(ValueError, "review arquivado|hash divergente"):
                            recover_source_end("test", "123", "br", date(2025, 1, 1), source, checkpoint)

    def test_recovery_rejects_source_journal_mismatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.RAW_ROOT", root / "raw"), \
                 patch("scripts.collect.app_store_web.SNAPSHOT_ROOT", root / "snapshots"):
                source = self.make_source_end(root)
                checkpoint = recovery_fixture(root, source)
                data = json.loads(checkpoint.read_text())
                data["source_journal_sha256"] = "0" * 64
                checkpoint.write_text(json.dumps(data))
                with patch("scripts.collect.app_store_web.urlopen", side_effect=AssertionError("No HTTP during import")):
                    with self.assertRaisesRegex(ValueError, "Hash divergente do source run"):
                        recover_source_end("test", "123", "br", date(2025, 1, 1), source, checkpoint)

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

    def test_continuity_anomaly_is_archived_and_resumable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = page(next_offset=10)
            bad = page(offset=10, next_offset=20)
            bad["data"][4]["id"] = "5"  # Seen earlier, but not at the page boundary.
            calls = []

            def fetch(request, timeout):
                calls.append(request.full_url)
                return Response([first, bad, page(offset=10, next_offset=20)][len(calls) - 1])

            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=fetch), \
                 patch("scripts.collect.app_store_web.time.sleep"):
                paused = collect_web("test", "123", "br", delay_seconds=4, max_pages=2)
                state = replay_run(paused, "123", "br")
                self.assertEqual([entry["offset"] for entry in state["confirmed"]], [0])
                self.assertEqual(state["next_url"].split("offset=")[1].split("&")[0], "10")
                self.assertEqual(json.loads((paused / "summary.json").read_text())["stop_reason"], "continuity_uncertain")
                self.assertEqual(len(state["ids"]), 10)
                self.assertEqual(sha256(paused / state["entries"][-1]["body_file"]), state["entries"][-1]["body_sha256"])
                resumed = collect_web("test", "123", "br", delay_seconds=4, max_pages=1, resume_from=paused)
                self.assertEqual([entry["offset"] for entry in replay_run(resumed, "123", "br")["confirmed"]], [0, 10])
            self.assertEqual(calls[-2:], [calls[-1]] * 2)

    def test_adjacent_prefix_overlap_is_confirmed_and_deduplicated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = page(next_offset=10)
            overlap = page(offset=10, next_offset=20)
            overlap["data"][:3] = first["data"][-3:]
            responses = iter([Response(first), Response(overlap)])
            with patch("scripts.collect.app_store_web.RUNS_ROOT", root / "runs"), \
                 patch("scripts.collect.app_store_web.STAGING_ROOT", root / "staging"), \
                 patch("scripts.collect.app_store_web.urlopen", side_effect=lambda request, timeout: next(responses)), \
                 patch("scripts.collect.app_store_web.time.sleep"):
                run = collect_web("test", "123", "br", delay_seconds=4, max_pages=2)
            state = replay_run(run, "123", "br")
            self.assertEqual(json.loads((run / "summary.json").read_text())["stop_reason"], "page_limit")
            self.assertEqual([entry["offset"] for entry in state["confirmed"]], [0, 10])
            self.assertEqual(state["confirmed"][-1]["overlap_ids"], ["7", "8", "9"])
            self.assertEqual(len(state["ids"]), 17)
            self.assertEqual(len(build_web_frame(run, "123", "br")), 17)


if __name__ == "__main__":
    unittest.main()
