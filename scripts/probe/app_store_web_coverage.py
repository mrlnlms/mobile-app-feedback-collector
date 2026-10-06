"""Follow Inter App Store web `sort=recent` from offset zero until the 2025 boundary.

Usage:
    venv/bin/python -m scripts.probe.app_store_web_coverage --resume-from PREVIOUS_RUN

Every successful response is written before the next request. This is an
experiment; it never publishes a canonical review base.
"""

import argparse
import json
import os
import shutil
import time
import uuid
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from scripts.collect.storage import assert_available, assert_local, sha256
from scripts.probe.app_store_web import (
    CANONICAL, DEFAULT_OUTPUT, FIRST_URL, HEADERS, STAGING_ROOT,
    extract_reviews, normalize_next, publish_experiment, save_json, timestamp,
)


CUTOFF = date(2025, 1, 1)


def parsed_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def offset_from_url(url):
    values = parse_qs(urlsplit(url).query).get("offset", ["0"])
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError(f"Offset inválido: {url}")
    return int(values[0])


def inspect_page(payload, offset, seen_ids, previous_oldest):
    reviews = extract_reviews(payload)
    ids = [review["id_review"] for review in reviews]
    dates = [parsed_date(review["web_date"]) for review in reviews]
    duplicate_ids = sorted({review_id for review_id in ids if review_id in seen_ids or ids.count(review_id) > 1})
    anomalies = []
    if duplicate_ids:
        anomalies.append("duplicate_ids")
    if any(left < right for left, right in zip(dates, dates[1:])):
        anomalies.append("date_increase_within_page")
    if dates and previous_oldest and dates[0] > parsed_date(previous_oldest):
        anomalies.append("date_increase_across_pages")
    next_raw = payload.get("next")
    next_url = None
    next_offset = None
    if next_raw:
        next_url = normalize_next(next_raw, sort="recent")
        next_offset = offset_from_url(next_url)
        if next_offset != offset + len(reviews):
            anomalies.append("next_offset_jump")
        if not reviews:
            anomalies.append("empty_with_next")
        elif len(reviews) != 10:
            anomalies.append("short_page_with_next")
    return {
        "offset": offset, "review_count": len(reviews),
        "first_id": ids[0] if ids else None, "last_id": ids[-1] if ids else None,
        "date_newest": max(dates).isoformat() if dates else None,
        "date_oldest": min(dates).isoformat() if dates else None,
        "duplicate_ids": duplicate_ids,
        "next_present": bool(next_raw), "next_raw": next_raw,
        "next_url": next_url, "next_offset": next_offset,
        "before_cutoff": any(value.date() < CUTOFF for value in dates),
        "anomalies": anomalies, "ids": ids,
    }


def append_journal(path, entry):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def retry_after_seconds(value, now=None):
    """Return the server's requested delay, or None for an unusable header."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        return max(0.0, (target - (now or datetime.now(timezone.utc))).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


def load_confirmed_state(run):
    """Replay the journal, validating raw hashes and only advancing on HTTP 200."""
    run = Path(run)
    entries = [json.loads(line) for line in (run / "pages.jsonl").read_text(encoding="utf-8").splitlines()]
    seen_ids = set()
    requested_urls = set()
    dates_min = []
    dates_max = []
    previous_oldest = None
    next_url = FIRST_URL + "&sort=recent"
    pages_200 = 0
    for entry in entries:
        if entry.get("body_file"):
            body = run / entry["body_file"]
            if not body.is_file() or sha256(body) != entry["body_sha256"]:
                raise ValueError(f"Corpo bruto ausente ou hash divergente: {body}")
        if entry.get("http_status") != 200:
            continue
        if entry["url"] != next_url or entry["url"] in requested_urls:
            raise ValueError(f"Sequência confirmada inconsistente no offset {entry.get('offset')}")
        payload = json.loads((run / entry["body_file"]).read_bytes())
        check = inspect_page(payload, entry["offset"], seen_ids, previous_oldest)
        if check["anomalies"] or check["ids"] != entry.get("ids"):
            raise ValueError(f"Página confirmada inconsistente no offset {entry['offset']}")
        pages_200 += 1
        requested_urls.add(entry["url"])
        seen_ids.update(check["ids"])
        if check["date_oldest"]:
            dates_min.append(parsed_date(check["date_oldest"]))
            dates_max.append(parsed_date(check["date_newest"]))
            previous_oldest = check["date_oldest"]
        next_url = check["next_url"]
    return {
        "entries": entries, "pages_attempted": len(entries), "pages_http_200": pages_200,
        "seen_ids": seen_ids, "requested_urls": requested_urls,
        "dates_min": dates_min, "dates_max": dates_max,
        "previous_oldest": previous_oldest, "next_url": next_url,
        "next_offset": offset_from_url(next_url) if next_url else None,
    }


def run_coverage(delay_seconds=15.0, max_pages=1000, output_parent=DEFAULT_OUTPUT,
                 resume_from=None, max_429_retries=3):
    if delay_seconds < 4 or max_pages < 1 or max_429_retries < 0:
        raise ValueError("Use pausa mínima de 4 segundos e limites não negativos")
    assert_available(CANONICAL)
    assert_available(output_parent)
    if not CANONICAL.is_file():
        raise FileNotFoundError(f"Base RSS canônica ausente: {CANONICAL}")
    assert_local((STAGING_ROOT,), output_parent)
    canonical_before = sha256(CANONICAL)
    run_name = ("inter_web_recent_resumed_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
                + "_" + uuid.uuid4().hex[:6])
    if resume_from is None:
        staging = STAGING_ROOT / run_name
        (staging / "pages").mkdir(parents=True, exist_ok=False)
        state = None
    else:
        source = Path(resume_from)
        state = load_confirmed_state(source)
        if not state["next_url"]:
            raise ValueError("Rodada anterior não indica próxima página")
        if source.resolve().parent == STAGING_ROOT.resolve():
            staging = source
        else:
            staging = STAGING_ROOT / run_name
            shutil.copytree(source, staging)
    journal = staging / "pages.jsonl"
    next_url = state["next_url"] if state else FIRST_URL + "&sort=recent"
    requested_urls = state["requested_urls"] if state else set()
    seen_ids = state["seen_ids"] if state else set()
    dates_min = state["dates_min"] if state else []
    dates_max = state["dates_max"] if state else []
    previous_oldest = state["previous_oldest"] if state else None
    stop_reason = "page_limit"
    started_at = timestamp()
    pages_200 = state["pages_http_200"] if state else 0
    pages_attempted = state["pages_attempted"] if state else 0
    new_pages_200 = 0
    retry_streak = 0
    anomaly_details = []
    rate_limit_attempts = 0

    def save_progress():
        save_json(staging / "progress.json", {
            "started_at": started_at, "updated_at": timestamp(),
            "resume_source": str(resume_from) if resume_from else None,
            "pages_attempted": pages_attempted, "pages_http_200": pages_200,
            "last_confirmed_offset": max((offset_from_url(url) for url in requested_urls), default=None),
            "next_url": next_url, "next_offset": offset_from_url(next_url) if next_url else None,
            "unique_ids": len(seen_ids),
            "date_oldest": min(dates_min).isoformat() if dates_min else None,
        })

    try:
        while new_pages_200 < max_pages:
            if next_url in requested_urls:
                stop_reason = "repeated_next_url"
                break
            offset = offset_from_url(next_url)
            page_number = pages_attempted + 1
            entry = {"page": page_number, "offset": offset, "url": next_url,
                     "requested_at": timestamp(), "retry_number": retry_streak}
            body = b""
            try:
                with urlopen(Request(next_url, headers=HEADERS), timeout=30) as response:
                    entry["http_status"] = response.status
                    entry["content_type"] = response.headers.get("Content-Type")
                    body = response.read(10_000_001)
                if len(body) > 10_000_000:
                    raise ValueError("Resposta maior que 10 MB")
            except HTTPError as exc:
                entry["http_status"] = exc.code
                entry["content_type"] = exc.headers.get("Content-Type")
                entry["retry_after_header"] = exc.headers.get("Retry-After")
                entry["error"] = str(exc.reason)
                try:
                    body = exc.read(10_000_001)
                finally:
                    exc.close()
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                entry["error"] = str(exc)
                stop_reason = "request_error"
            pages_attempted += 1
            if body:
                suffix = ".json" if entry.get("http_status") == 200 else ".body"
                # A crash before the journal append leaves an orphan body; a
                # resumed request must never overwrite that evidence.
                path = staging / "pages" / f"{page_number:04d}_{uuid.uuid4().hex[:8]}{suffix}"
                with path.open("wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                entry["body_file"] = str(path.relative_to(staging))
                entry["body_sha256"] = sha256(path)
                entry["body_bytes"] = len(body)
            entry["received_at"] = timestamp()
            if entry.get("http_status") == 429:
                rate_limit_attempts += 1
                retry_streak += 1
                header_wait = retry_after_seconds(entry.get("retry_after_header"))
                wait = max(delay_seconds, header_wait) if header_wait is not None else max(delay_seconds, min(60 * 2 ** (retry_streak - 1), 600))
                entry["suggested_backoff_seconds"] = wait
                entry["backoff_source"] = "retry_after" if header_wait is not None else "exponential"
                entry["will_retry"] = retry_streak <= max_429_retries
                entry["backoff_seconds"] = wait if entry["will_retry"] else 0.0
            if entry.get("http_status") != 200 or stop_reason == "request_error":
                entry.update({"review_count": 0, "first_id": None, "last_id": None,
                              "date_newest": None, "date_oldest": None,
                              "duplicate_ids": [], "next_present": False, "next_raw": None,
                              "next_url": None, "next_offset": None, "anomalies": []})
                append_journal(journal, entry)
                save_progress()
                if entry.get("http_status") == 429:
                    if entry["will_retry"]:
                        print(f"HTTP 429 no offset {offset}; aguardando {wait:.0f}s antes de repetir", flush=True)
                        time.sleep(wait)
                        continue
                    stop_reason = "rate_limited_paused"
                elif stop_reason != "request_error":
                    stop_reason = f"http_{entry.get('http_status')}"
                break
            try:
                payload = json.loads(body)
                inspection = inspect_page(payload, offset, seen_ids, previous_oldest)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                entry["error"] = str(exc)
                entry["anomalies"] = ["invalid_response"]
                append_journal(journal, entry)
                stop_reason = "invalid_response"
                break
            entry.update(inspection)
            append_journal(journal, entry)
            pages_200 += 1
            new_pages_200 += 1
            retry_streak = 0
            requested_urls.add(next_url)
            seen_ids.update(inspection["ids"])
            if inspection["date_oldest"]:
                dates_min.append(parsed_date(inspection["date_oldest"]))
                dates_max.append(parsed_date(inspection["date_newest"]))
                previous_oldest = inspection["date_oldest"]
            next_url = inspection["next_url"]
            save_progress()
            if pages_200 % 25 == 0:
                print(f"{pages_200} páginas; {len(seen_ids)} IDs; data mínima {previous_oldest}", flush=True)
            if inspection["anomalies"]:
                anomaly_details = inspection["anomalies"]
                stop_reason = "continuity_uncertain"
                break
            if inspection["before_cutoff"]:
                stop_reason = "pre_2025_reached"
                break
            if not inspection["next_present"]:
                stop_reason = "source_end"
                break
            if new_pages_200 < max_pages:
                time.sleep(delay_seconds)
        canonical_after = sha256(CANONICAL)
        if canonical_after != canonical_before:
            raise RuntimeError("Base RSS canônica mudou durante o experimento")
        summary = {
            "app_id": "839711154", "storefront": "br", "sort": "recent",
            "started_at": started_at, "finished_at": timestamp(),
            "delay_seconds": delay_seconds, "max_pages": max_pages,
            "pages_attempted": pages_attempted, "pages_http_200": pages_200,
            "new_pages_http_200": new_pages_200, "rate_limit_attempts_this_run": rate_limit_attempts,
            "resume_source": str(resume_from) if resume_from else None,
            "last_confirmed_offset": max((offset_from_url(url) for url in requested_urls), default=None),
            "next_offset": offset_from_url(next_url) if next_url else None,
            "unique_ids": len(seen_ids), "date_min": min(dates_min).isoformat() if dates_min else None,
            "date_max": max(dates_max).isoformat() if dates_max else None,
            "stop_reason": stop_reason, "anomaly_details": anomaly_details,
            "cutoff_reached": stop_reason == "pre_2025_reached",
            "canonical_sha256_before": canonical_before,
            "canonical_sha256_after": canonical_after,
        }
        save_json(staging / "summary.json", summary)
        save_json(staging / "ids.json", sorted(seen_ids))
        return publish_experiment(staging, Path(output_parent))
    except BaseException:
        # Raw bodies and an fsynced page journal remain in local staging.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-seconds", type=float, default=15.0)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--max-429-retries", type=int, default=3)
    args = parser.parse_args()
    result = run_coverage(args.delay_seconds, args.max_pages,
                          resume_from=args.resume_from, max_429_retries=args.max_429_retries)
    print(result)
    print((result / "summary.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
