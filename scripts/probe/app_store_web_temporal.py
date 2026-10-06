"""Audit raw RSS/Web time fields for the preserved Inter experiments only."""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts.collect.storage import assert_available, assert_local, sha256
from scripts.probe.app_store_web import (
    CANONICAL, DEFAULT_OUTPUT, STAGING_ROOT, extract_reviews,
    publish_experiment, save_json, timestamp,
)
from scripts.probe.app_store_web_analysis import run_name


def read_entries(run):
    journal = Path(run) / "pages.jsonl"
    if journal.is_file():
        return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    return json.loads((Path(run) / "requests.json").read_text(encoding="utf-8"))


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp sem offset: {value}")
    return parsed.astimezone(timezone.utc)


def compare_source_times(rss_updated_raw, web_date_raw):
    rss_utc = parse_utc(rss_updated_raw)
    web_utc = parse_utc(web_date_raw)
    return {
        "rss_updated_raw": rss_updated_raw,
        "rss_updated_utc": rss_utc.isoformat(),
        "web_date_raw": web_date_raw,
        "web_date_utc": web_utc.isoformat(),
        "rss_minus_web_seconds": int((rss_utc - web_utc).total_seconds()),
    }


def run_temporal_audit(web_runs, rss_captures, output_parent=DEFAULT_OUTPUT):
    """Keep first raw value per review ID and flag conflicting later observations."""
    assert_available(CANONICAL)
    assert_available(output_parent)
    assert_local((STAGING_ROOT,), output_parent)
    canonical_before = sha256(CANONICAL)
    web = {}
    rss = {}
    web_conflicts = set()
    rss_conflicts = set()
    for root in map(Path, web_runs):
        for request in read_entries(root):
            if request.get("http_status") != 200 or not request.get("body_file"):
                continue
            path = root / request["body_file"]
            if sha256(path) != request["body_sha256"]:
                raise ValueError(f"Hash Web divergente: {path}")
            for review in extract_reviews(json.loads(path.read_bytes())):
                review_id = review["id_review"]
                observation = {"fields": review, "run": str(root), "body_file": request["body_file"]}
                if review_id in web and web[review_id]["fields"] != review:
                    web_conflicts.add(review_id)
                web.setdefault(review_id, observation)
    for root in map(Path, rss_captures):
        for request in read_entries(root):
            if request.get("http_status") != 200 or not request.get("body_file"):
                continue
            path = root / request["body_file"]
            if sha256(path) != request["body_sha256"]:
                raise ValueError(f"Hash RSS divergente: {path}")
            payload = json.loads(path.read_bytes())
            for entry in payload.get("feed", {}).get("entry", []):
                if "im:rating" not in entry:
                    continue
                review_id = entry["id"]["label"]
                fields = {
                    "updated": entry["updated"]["label"],
                    "text": entry["content"]["label"],
                    "title": entry["title"]["label"],
                    "rating": int(entry["im:rating"]["label"]),
                    "version": entry.get("im:version", {}).get("label"),
                }
                observation = {"fields": fields, "run": str(root), "body_file": request["body_file"]}
                if review_id in rss and rss[review_id]["fields"] != fields:
                    rss_conflicts.add(review_id)
                rss.setdefault(review_id, observation)
    pairs = {}
    deltas = Counter()
    edited_deltas = Counter()
    content_mismatch = []
    for review_id in sorted(set(web) & set(rss)):
        web_fields = web[review_id]["fields"]
        rss_fields = rss[review_id]["fields"]
        pair = compare_source_times(rss_fields["updated"], web_fields["web_date"])
        pair.update({
            "web_is_edited": web_fields["is_edited"],
            "rss_version": rss_fields["version"],
            "text_equal": rss_fields["text"] == web_fields["text"],
            "title_equal": rss_fields["title"] == web_fields["title"],
            "rating_equal": rss_fields["rating"] == web_fields["rating"],
            "rss_source": {key: rss[review_id][key] for key in ("run", "body_file")},
            "web_source": {key: web[review_id][key] for key in ("run", "body_file")},
        })
        pairs[review_id] = pair
        deltas[str(pair["rss_minus_web_seconds"])] += 1
        edited_deltas[f"{web_fields['is_edited']}:{pair['rss_minus_web_seconds']}"] += 1
        if not all(pair[key] for key in ("text_equal", "title_equal", "rating_equal")):
            content_mismatch.append(review_id)
    canonical_after = sha256(CANONICAL)
    if canonical_before != canonical_after:
        raise RuntimeError("Base RSS canônica mudou durante a análise")
    staging = STAGING_ROOT / run_name("inter_web_temporal_semantics_")
    staging.mkdir(parents=True, exist_ok=False)
    save_json(staging / "pairs.json", pairs)
    save_json(staging / "summary.json", {
        "app_id": "839711154", "web_runs": [str(path) for path in web_runs],
        "rss_captures": [str(path) for path in rss_captures],
        "web_ids": len(web), "rss_ids": len(rss), "shared_ids": len(pairs),
        "deltas_seconds": dict(sorted(deltas.items(), key=lambda item: int(item[0]))),
        "edited_deltas": dict(sorted(edited_deltas.items())),
        "larger_than_one_hour_ids": [key for key, value in pairs.items()
                                     if abs(value["rss_minus_web_seconds"]) > 3600],
        "content_mismatch_ids": content_mismatch,
        "web_conflicting_ids": sorted(web_conflicts), "rss_conflicting_ids": sorted(rss_conflicts),
        "canonical_sha256_before": canonical_before, "canonical_sha256_after": canonical_after,
        "finished_at": timestamp(),
    })
    return publish_experiment(staging, Path(output_parent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-run", type=Path, action="append", required=True)
    parser.add_argument("--rss-capture", type=Path, action="append", required=True)
    args = parser.parse_args()
    result = run_temporal_audit(args.web_run, args.rss_capture)
    print(result)
    print((result / "summary.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
