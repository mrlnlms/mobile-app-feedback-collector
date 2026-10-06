"""Read-only Inter Web/RSS set and timestamp analysis.

Usage:
    venv/bin/python -m scripts.probe.app_store_web_analysis capture-rss
    venv/bin/python -m scripts.probe.app_store_web_analysis compare WEB_RUN RSS_CAPTURE
"""

import argparse
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.collect.storage import assert_available, assert_local, sha256
from scripts.probe.app_store_web import (
    APP_ID, CANONICAL, DEFAULT_OUTPUT, STAGING_ROOT, extract_reviews,
    publish_experiment, save_json, timestamp,
)


RSS_URL = "https://itunes.apple.com/br/rss/customerreviews/page={page}/id={app_id}/sortby={sort}/json"
PACIFIC = ZoneInfo("America/Los_Angeles")


def parse_rss_reviews(payload):
    feed = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(feed, dict):
        raise ValueError("RSS sem feed")
    entries = feed.get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        raise ValueError("RSS sem lista entry")
    result = {}
    for entry in entries:
        if "im:rating" not in entry:
            continue
        review_id = entry.get("id", {}).get("label")
        raw_updated = entry.get("updated", {}).get("label")
        if not isinstance(review_id, str) or not isinstance(raw_updated, str):
            raise ValueError("RSS review sem ID ou updated")
        result[review_id] = raw_updated
    return result


def compare_id_sets(web_ids, rss_ids):
    return {
        "web_and_rss": set(web_ids) & set(rss_ids),
        "web_only": set(web_ids) - set(rss_ids),
        "rss_only": set(rss_ids) - set(web_ids),
    }


def temporal_pair(rss_updated_raw, web_date_raw, parquet_date):
    rss = datetime.fromisoformat(rss_updated_raw.replace("Z", "+00:00"))
    web = datetime.fromisoformat(web_date_raw.replace("Z", "+00:00"))
    stored = pd.Timestamp(parquet_date).to_pydatetime()
    if any(value.tzinfo is None for value in (rss, web, stored)):
        raise ValueError("Data sem timezone")
    rss_utc = rss.astimezone(timezone.utc)
    web_utc = web.astimezone(timezone.utc)
    stored_utc = stored.astimezone(timezone.utc)
    pacific = web_utc.astimezone(PACIFIC)
    return {
        "rss_updated_raw": rss_updated_raw,
        "rss_parsed_utc": rss_utc.isoformat(),
        "web_date_raw": web_date_raw,
        "web_parsed_utc": web_utc.isoformat(),
        "parquet_utc": stored_utc.isoformat(),
        "rss_minus_web_seconds": int((rss_utc - web_utc).total_seconds()),
        "rss_offset_seconds": int(rss.utcoffset().total_seconds()),
        "los_angeles_offset_seconds": int(pacific.utcoffset().total_seconds()),
        "rss_local_wall_clock": rss.replace(tzinfo=None).isoformat(),
        "web_los_angeles_wall_clock": pacific.replace(tzinfo=None).isoformat(),
        "same_local_wall_clock": rss.replace(tzinfo=None) == pacific.replace(tzinfo=None),
        "rss_parse_matches_parquet": rss_utc == stored_utc,
    }


def run_name(prefix):
    return prefix + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:6]


def capture_rss(max_pages=10, delay_seconds=3.0, output_parent=DEFAULT_OUTPUT,
                sort="mosthelpful"):
    if not 1 <= max_pages <= 10 or delay_seconds < 2:
        raise ValueError("Use 1–10 páginas e pausa mínima de 2 segundos")
    if sort not in {"mosthelpful", "mostrecent"}:
        raise ValueError(f"Sort RSS inválido: {sort}")
    assert_available(CANONICAL)
    assert_available(output_parent)
    assert_local((STAGING_ROOT,), output_parent)
    canonical_before = sha256(CANONICAL)
    staging = STAGING_ROOT / run_name(f"inter_rss_{sort}_temporal_")
    (staging / "pages").mkdir(parents=True, exist_ok=False)
    requests = []
    observed_ids = set()
    stop_reason = "page_limit"
    for page in range(1, max_pages + 1):
        url = RSS_URL.format(page=page, app_id=APP_ID, sort=sort)
        entry = {"page": page, "url": url, "requested_at": timestamp()}
        try:
            with urlopen(Request(url, headers={"User-Agent": "playstore-feedbacks/1.0"}), timeout=30) as response:
                entry["http_status"] = response.status
                body = response.read(10_000_001)
        except HTTPError as exc:
            entry["http_status"] = exc.code
            entry["error"] = str(exc.reason)
            body = exc.read(10_000_001)
            stop_reason = f"http_{exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            entry["error"] = str(exc)
            body = b""
            stop_reason = "request_error"
        if body:
            path = staging / "pages" / f"{page:04d}{'.json' if entry.get('http_status') == 200 else '.body'}"
            path.write_bytes(body)
            entry["body_file"] = str(path.relative_to(staging))
            entry["body_sha256"] = sha256(path)
            entry["body_bytes"] = len(body)
        entry["received_at"] = timestamp()
        if stop_reason.startswith("http_") or stop_reason == "request_error":
            requests.append(entry)
            break
        try:
            raw = parse_rss_reviews(json.loads(body))
        except (ValueError, json.JSONDecodeError) as exc:
            entry["error"] = str(exc)
            requests.append(entry)
            stop_reason = "invalid_response"
            break
        entry["review_count"] = len(raw)
        entry["ids"] = list(raw)
        requests.append(entry)
        observed_ids.update(raw)
        save_json(staging / "requests.json", requests)
        if not raw:
            stop_reason = "empty_page"
            break
        if page < max_pages:
            time.sleep(delay_seconds)
    canonical_after = sha256(CANONICAL)
    if canonical_before != canonical_after:
        raise RuntimeError("Base canônica mudou durante a captura RSS")
    save_json(staging / "requests.json", requests)
    save_json(staging / "summary.json", {
        "app_id": APP_ID, "sort": sort, "pages_attempted": len(requests),
        "observed_ids": len(observed_ids), "stop_reason": stop_reason,
        "canonical_sha256_before": canonical_before, "canonical_sha256_after": canonical_after,
        "finished_at": timestamp(),
    })
    return publish_experiment(staging, Path(output_parent))


def load_web_reviews(web_run):
    reviews = {}
    journal = Path(web_run) / "pages.jsonl"
    if journal.is_file():
        entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    else:
        entries = json.loads((Path(web_run) / "requests.json").read_text(encoding="utf-8"))
    for entry in entries:
        if entry.get("http_status") != 200 or not entry.get("body_file"):
            continue
        payload = json.loads((Path(web_run) / entry["body_file"]).read_bytes())
        for review in extract_reviews(payload):
            reviews.setdefault(review["id_review"], review)
    return reviews


def load_rss_raw(rss_capture):
    raw = {}
    conflicts = []
    requests = json.loads((Path(rss_capture) / "requests.json").read_text(encoding="utf-8"))
    for entry in requests:
        if entry.get("http_status") != 200 or not entry.get("body_file"):
            continue
        payload = json.loads((Path(rss_capture) / entry["body_file"]).read_bytes())
        for review_id, updated in parse_rss_reviews(payload).items():
            if review_id in raw and raw[review_id] != updated:
                conflicts.append(review_id)
            raw.setdefault(review_id, updated)
    return raw, sorted(set(conflicts))


def run_comparison(web_run, rss_captures, output_parent=DEFAULT_OUTPUT):
    assert_available(CANONICAL)
    assert_available(output_parent)
    assert_local((STAGING_ROOT,), output_parent)
    canonical_before = sha256(CANONICAL)
    web = load_web_reviews(web_run)
    frame = pd.read_parquet(CANONICAL)
    if frame["id_review"].astype(str).duplicated().any():
        raise ValueError("Base RSS com IDs duplicados")
    rss = {str(row.id_review): row for row in frame.itertuples(index=False)}
    sets = compare_id_sets(web, rss)
    if isinstance(rss_captures, (str, Path)):
        rss_captures = [rss_captures]
    rss_raw = {}
    raw_conflicts = []
    for capture in rss_captures:
        observed, conflicts = load_rss_raw(capture)
        raw_conflicts.extend(conflicts)
        for review_id, updated in observed.items():
            if review_id in rss_raw and rss_raw[review_id] != updated:
                raw_conflicts.append(review_id)
            rss_raw.setdefault(review_id, updated)
    pairs = {}
    observed_deltas = Counter()
    for review_id in sets["web_and_rss"]:
        web_date = datetime.fromisoformat(web[review_id]["web_date"].replace("Z", "+00:00"))
        stored = pd.Timestamp(rss[review_id].data_avaliacao).to_pydatetime()
        delta = int((stored - web_date).total_seconds())
        observed_deltas[str(delta)] += 1
        if review_id in rss_raw:
            pairs[review_id] = temporal_pair(rss_raw[review_id], web[review_id]["web_date"],
                                             rss[review_id].data_avaliacao)
    affected = {review_id for review_id in sets["web_and_rss"]
                if int((pd.Timestamp(rss[review_id].data_avaliacao)
                        - pd.Timestamp(web[review_id]["web_date"])).total_seconds()) == -3600}
    period = {
        "web_and_rss_by_web_year": dict(sorted(Counter(web[i]["web_date"][:4] for i in sets["web_and_rss"]).items())),
        "web_only_by_web_year": dict(sorted(Counter(web[i]["web_date"][:4] for i in sets["web_only"]).items())),
        "rss_only_by_rss_year": dict(sorted(Counter(str(rss[i].data_avaliacao.year) for i in sets["rss_only"]).items())),
        "web_only_by_web_month": dict(sorted(Counter(web[i]["web_date"][:7] for i in sets["web_only"]).items())),
        "rss_only_by_rss_month": dict(sorted(Counter(rss[i].data_avaliacao.strftime("%Y-%m") for i in sets["rss_only"]).items())),
    }
    canonical_after = sha256(CANONICAL)
    if canonical_before != canonical_after:
        raise RuntimeError("Base RSS mudou durante a análise")
    staging = STAGING_ROOT / run_name("inter_web_rss_analysis_")
    staging.mkdir(parents=True, exist_ok=False)
    save_json(staging / "sets.json", {key: sorted(value) for key, value in sets.items()})
    save_json(staging / "temporal_pairs.json", pairs)
    save_json(staging / "periods.json", period)
    save_json(staging / "summary.json", {
        "web_run": str(web_run), "rss_captures": [str(path) for path in rss_captures],
        "web_ids": len(web), "rss_ids": len(rss),
        "web_and_rss": len(sets["web_and_rss"]), "web_only": len(sets["web_only"]),
        "rss_only": len(sets["rss_only"]),
        "temporal_delta_seconds": dict(sorted(observed_deltas.items())),
        "affected_one_hour_ids": len(affected),
        "affected_with_raw_rss": len(affected & set(rss_raw)),
        "affected_missing_raw_rss": sorted(affected - set(rss_raw)),
        "raw_rss_conflicting_ids": sorted(set(raw_conflicts)),
        "canonical_sha256_before": canonical_before,
        "canonical_sha256_after": canonical_after,
        "finished_at": timestamp(),
    })
    return publish_experiment(staging, Path(output_parent))


def run_union_comparison(web_runs, output_parent=DEFAULT_OUTPUT):
    """Compare the observed union of explicit Inter web runs with the read-only RSS base."""
    assert_available(CANONICAL)
    assert_available(output_parent)
    assert_local((STAGING_ROOT,), output_parent)
    canonical_before = sha256(CANONICAL)
    web = {}
    provenance = {}
    conflicting_ids = set()
    paths = [Path(path) for path in web_runs]
    if not paths:
        raise ValueError("Nenhuma rodada web fornecida")
    for path in paths:
        run_summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
        if str(run_summary.get("app_id")) != APP_ID or not (path / "pages").is_dir():
            raise ValueError(f"Não é uma rodada web do Inter: {path}")
        for review_id, review in load_web_reviews(path).items():
            if review_id in web and web[review_id] != review:
                conflicting_ids.add(review_id)
            web.setdefault(review_id, review)
            provenance.setdefault(review_id, []).append(path.name)
    frame = pd.read_parquet(CANONICAL)
    if frame["id_review"].astype(str).duplicated().any():
        raise ValueError("Base RSS com IDs duplicados")
    rss = {str(row.id_review): row for row in frame.itertuples(index=False)}
    sets = compare_id_sets(web, rss)
    periods = {
        "web_and_rss_by_web_year": dict(sorted(Counter(web[i]["web_date"][:4] for i in sets["web_and_rss"]).items())),
        "web_only_by_web_year": dict(sorted(Counter(web[i]["web_date"][:4] for i in sets["web_only"]).items())),
        "rss_only_by_rss_year": dict(sorted(Counter(str(rss[i].data_avaliacao.year) for i in sets["rss_only"]).items())),
        "rss_only_by_sort": dict(sorted(Counter(rss[i].sorts_encontrados for i in sets["rss_only"]).items())),
        "rss_only_by_rss_month": dict(sorted(Counter(rss[i].data_avaliacao.strftime("%Y-%m") for i in sets["rss_only"]).items())),
    }
    canonical_after = sha256(CANONICAL)
    if canonical_before != canonical_after:
        raise RuntimeError("Base RSS mudou durante a comparação")
    staging = STAGING_ROOT / run_name("inter_web_union_analysis_")
    staging.mkdir(parents=True, exist_ok=False)
    save_json(staging / "sets.json", {key: sorted(value) for key, value in sets.items()})
    save_json(staging / "periods.json", periods)
    save_json(staging / "web_provenance.json", provenance)
    save_json(staging / "summary.json", {
        "web_runs": [str(path) for path in paths],
        "web_ids": len(web), "rss_ids": len(rss),
        "web_and_rss": len(sets["web_and_rss"]),
        "web_only": len(sets["web_only"]), "rss_only": len(sets["rss_only"]),
        "web_field_conflicting_ids": sorted(conflicting_ids),
        "canonical_sha256_before": canonical_before,
        "canonical_sha256_after": canonical_after,
        "finished_at": timestamp(),
    })
    return publish_experiment(staging, Path(output_parent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("capture-rss")
    collect.add_argument("--max-pages", type=int, default=10)
    collect.add_argument("--delay-seconds", type=float, default=3.0)
    collect.add_argument("--sort", choices=("mosthelpful", "mostrecent"), default="mosthelpful")
    analyze = sub.add_parser("compare")
    analyze.add_argument("web_run", type=Path)
    analyze.add_argument("rss_captures", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command == "capture-rss":
        result = capture_rss(args.max_pages, args.delay_seconds, sort=args.sort)
    else:
        result = run_comparison(args.web_run, args.rss_captures)
    print(result)
    print((result / "summary.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
