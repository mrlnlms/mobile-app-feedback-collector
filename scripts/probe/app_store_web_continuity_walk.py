"""Preserve a bounded direct-offset walk after an App Store Web continuity pause.

This probe never publishes a canonical review base. It writes raw responses and
produces a recovery checkpoint only after crossing the last confirmed ID and
observing another page of older, new IDs.
"""

import argparse
import hashlib
import json
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from scripts.collect.app_store_web import (
    HEADERS, RAW_ROOT, archive_web_staging, extract_reviews,
    inspect_recovery_page, offset_from_url, replay_run, retry_after_seconds,
    url_at_offset, utc_now,
)
from scripts.collect.storage import assert_available, assert_local, save_json, sha256


STAGING_ROOT = Path(".runtime/app_store/web_probe")
OUTPUT_ROOT = Path("data/runs/app_store/experiments")


def walk(bank, source_run, delay_seconds=20, max_pages=20, max_429_retries=3):
    if delay_seconds < 4 or max_pages < 2 or max_429_retries < 0:
        raise ValueError("Cadência mínima de 4 s, ao menos 2 páginas e retries não negativos")
    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    app_id = str(config["banks"][bank]["apple_app_id"])
    storefront = config["app_store"]["country"]
    cutoff = date.fromisoformat(config["banks"][bank]["start_date"])
    source_run = Path(source_run)
    summary = json.loads((source_run / "summary.json").read_text(encoding="utf-8"))
    if summary["stop_reason"] != "continuity_uncertain":
        raise ValueError("Source run não terminou em continuity_uncertain")
    prior = replay_run(source_run, app_id, storefront, cutoff)
    if not prior["next_url"] or prior["entries"][-1].get("offset") != offset_from_url(prior["next_url"]):
        raise ValueError("Source run sem anomalia no offset pendente")
    canonical = RAW_ROOT / bank / "reviews_web.parquet"
    assert_available(canonical)
    assert_available(OUTPUT_ROOT)
    assert_local((STAGING_ROOT,), OUTPUT_ROOT)
    canonical_sha = sha256(canonical)

    archived_rows = {}
    archived_positions = {}
    for entry in prior["confirmed"]:
        payload = json.loads((source_run / entry["body_file"]).read_bytes())
        for row in extract_reviews(payload):
            review_id = row["id_review"]
            if review_id not in archived_rows:
                archived_positions[review_id] = len(archived_positions)
                archived_rows[review_id] = row
    if len(archived_rows) != len(prior["ids"]):
        raise ValueError("Mapa de IDs diverge do replay")

    name = (f"{bank}_web_continuity_walk_"
            f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S_%fZ}_{uuid.uuid4().hex[:6]}")
    staging = STAGING_ROOT / name
    staging.mkdir(parents=True, exist_ok=False)
    destination = OUTPUT_ROOT / name
    current = prior["next_url"]
    previous_oldest = None
    recovery_seen = set()
    last_position = -1
    tail_seen = False
    new_started = False
    novel_pages = 0
    new_ids = []
    pages = []
    rate_limits = []
    attempts = 0
    retries = 0
    stop_reason = "page_limit"
    next_request_at = None
    for _ in range(max_pages + max_429_retries * max_pages):
        if len(pages) >= max_pages:
            break
        if next_request_at is not None:
            time.sleep(max(0, next_request_at - time.monotonic()))
        offset = offset_from_url(current)
        attempts += 1
        folder = staging / f"page_{attempts:02d}"
        folder.mkdir()
        requested_at = utc_now()
        status = None
        content_type = None
        retry_after = None
        body = b""
        error = None
        try:
            with urlopen(Request(current, headers=HEADERS), timeout=30) as response:
                status = response.status
                content_type = response.headers.get("Content-Type")
                body = response.read(10_000_001)
        except HTTPError as exc:
            status = exc.code
            content_type = exc.headers.get("Content-Type")
            retry_after = exc.headers.get("Retry-After")
            error = str(exc.reason)
            body = exc.read(10_000_001)
            exc.close()
        except (URLError, TimeoutError, OSError) as exc:
            error = str(exc)
        received_at = utc_now()
        if len(body) > 10_000_000:
            error = "Resposta maior que 10 MB"
            status = None
        body_path = folder / "response.body"
        with body_path.open("wb") as stream:
            stream.write(body)
            stream.flush()
        metadata = {
            "offset": offset, "url": current, "requested_at": requested_at,
            "received_at": received_at, "http_status": status,
            "content_type": content_type, "retry_after": retry_after,
            "body_file": "response.body", "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body), "error": error,
        }
        save_json(folder / "probe.json", metadata)
        print(f"offset={offset} HTTP={status} sha256={metadata['body_sha256']}", flush=True)
        if status == 429:
            rate_limits.append({
                "offset": offset, "probe_dir": str(destination / folder.name),
                "body_file": str(destination / folder.name / "response.body"),
                "body_sha256": metadata["body_sha256"],
            })
            retries += 1
            if retries > max_429_retries:
                stop_reason = "rate_limited_paused"
                break
            header_wait = retry_after_seconds(retry_after)
            wait = max(delay_seconds, header_wait if header_wait is not None else
                       min(60 * 2 ** (retries - 1), 600))
            next_request_at = time.monotonic() + wait
            continue
        if status != 200:
            stop_reason = f"http_{status}" if status else "request_error"
            break
        retries = 0
        try:
            payload = json.loads(body)
            check = inspect_recovery_page(
                payload, offset, app_id, storefront, archived_rows,
                archived_positions, prior["previous_ids"][-1],
                prior["previous_oldest"], previous_oldest, recovery_seen,
                last_position, tail_seen, new_started)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            stop_reason = "continuity_uncertain"
            metadata["validation_error"] = str(exc)
            save_json(folder / "probe.json", metadata)
            print(f"Validação interrompida: {exc}", flush=True)
            break
        recovery_seen.update(check["ids"])
        previous_oldest = check["date_oldest"]
        last_position = check["last_archived_position"]
        tail_seen = check["tail_seen"]
        new_started = check["new_started"]
        new_ids.extend(check["recovery_new_ids"])
        pages.append({
            "offset": offset, "probe_dir": str(destination / folder.name),
            "body_file": str(destination / folder.name / "response.body"),
            "body_sha256": metadata["body_sha256"],
        })
        metadata.update({
            "recovery_existing_ids": check["recovery_existing_ids"],
            "recovery_new_ids": check["recovery_new_ids"],
            "date_oldest": check["date_oldest"], "next_offset": check["next_offset"],
        })
        save_json(folder / "probe.json", metadata)
        print(f"  antigos={len(check['recovery_existing_ids'])} novos={len(check['recovery_new_ids'])} "
              f"último_antigo={tail_seen} data={previous_oldest}", flush=True)
        current = check["next_url"]
        if new_started:
            novel_pages += 1
            if novel_pages >= 2 and len(check["recovery_new_ids"]) == 10:
                stop_reason = "bridge_confirmed"
                break
        next_request_at = time.monotonic() + delay_seconds

    record = {
        "kind": "app_store_web_continuity_recovery_probe", "version": 1,
        "bank": bank, "app_id": app_id, "storefront": storefront,
        "source_run": str(source_run),
        "source_journal_sha256": sha256(source_run / "pages.jsonl"),
        "canonical_web_parquet": str(canonical),
        "canonical_web_sha256": canonical_sha,
        "source_last_offset": prior["last_offset"],
        "direct_probe_first_offset": offset_from_url(prior["next_url"]),
        "last_probe_offset": pages[-1]["offset"] if pages else None,
        "next_offset": offset_from_url(current) if pages else None,
        "next_url": current if pages else None,
        "new_review_ids": new_ids, "probe_pages": pages,
        "rate_limit_responses": rate_limits, "stop_reason": stop_reason,
        "attempts": attempts, "date_oldest": previous_oldest,
    }
    if sha256(canonical) != canonical_sha:
        raise RuntimeError("Base canônica mudou durante o probe")
    save_json(staging / "recovery-summary.json", record)
    archived = archive_web_staging(staging, OUTPUT_ROOT)
    print(f"experimento={archived} stop_reason={stop_reason}", flush=True)
    return archived, stop_reason


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--delay-seconds", type=float, default=20)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-429-retries", type=int, default=3)
    args = parser.parse_args()
    walk(args.bank, args.source_run, args.delay_seconds,
         args.max_pages, args.max_429_retries)


if __name__ == "__main__":
    main()
