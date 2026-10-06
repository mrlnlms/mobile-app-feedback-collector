"""Measure the current App Store web reviews surface for Inter only.

Usage:
    venv/bin/python -m scripts.probe.app_store_web --max-pages 80
    venv/bin/python -m scripts.probe.app_store_web --sort recent --start-offset 4000 --max-pages 10
    venv/bin/python -m scripts.probe.app_store_web --compare RUN_1 RUN_2

The probe never writes to the canonical App Store Parquet.
"""

import argparse
import json
import os
import shutil
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import pandas as pd

from scripts.collect.storage import assert_available, assert_local, sha256


APP_ID = "839711154"
STOREFRONT = "br"
CONTROL_ID = "14355024152"
API_PATH = f"/v1/catalog/{STOREFRONT}/apps/{APP_ID}/reviews"
API_PREFIX = "https://apps.apple.com/api/apps"
FIRST_URL = f"{API_PREFIX}{API_PATH}?platform=web&l=pt-BR"
CANONICAL = Path("data/raw/app_store/inter/reviews_raw.parquet")
DEFAULT_OUTPUT = Path("data/runs/app_store/experiments")
STAGING_ROOT = Path(".runtime/app_store/web_probe")
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Accept-Language": "pt-BR"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def normalize_next(next_path, sort="default"):
    """Accept only Inter/br reviews and restore parameters omitted by the next link."""
    if sort not in {"default", "recent"}:
        raise ValueError(f"Sort não suportado pelo probe: {sort}")
    if not isinstance(next_path, str) or not next_path:
        raise ValueError("Next link ausente ou inválido")
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != API_PATH:
        raise ValueError(f"Next link fora da rota Inter/br: {next_path}")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key not in {"l", "offset", "platform", "sort"} for key, _ in pairs):
        raise ValueError(f"Next link com parâmetro inesperado: {next_path}")
    if any(key == "sort" and value != sort for key, value in pairs):
        raise ValueError(f"Next link mudou o sort: {next_path}")
    query = [(key, value) for key, value in pairs if key not in {"platform", "sort"}]
    query.append(("platform", "web"))
    if sort != "default":
        query.append(("sort", sort))
    return urlunsplit(("https", "apps.apple.com", "/api/apps" + API_PATH, urlencode(query), ""))


def extract_reviews(payload):
    """Extract identity and fields without assigning a publication-date meaning to web date."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Resposta sem lista data")
    reviews = []
    for row in payload["data"]:
        if not isinstance(row, dict) or row.get("type") != "user-reviews":
            raise ValueError("Item de review inesperado")
        attrs = row.get("attributes")
        if not isinstance(attrs, dict):
            raise ValueError("Review sem attributes")
        review_id, body, date_value = row.get("id"), attrs.get("review"), attrs.get("date")
        if not isinstance(review_id, str) or not review_id or not isinstance(body, str) or not body:
            raise ValueError("Review sem ID ou texto")
        if not isinstance(date_value, str):
            raise ValueError(f"Review {review_id} sem data")
        try:
            parsed_date = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Review {review_id} com data inválida") from exc
        if parsed_date.tzinfo is None:
            raise ValueError(f"Review {review_id} sem timezone")
        reviews.append({
            "id_review": review_id,
            "web_date": date_value,
            "text": body,
            "title": attrs.get("title"),
            "rating": attrs.get("rating"),
            "user_name": attrs.get("userName"),
            "is_edited": attrs.get("isEdited"),
        })
    return reviews


def repeated_page(current_ids, previous_ids):
    return bool(current_ids) and len(current_ids) == len(previous_ids) and set(current_ids) == set(previous_ids)


def publish_experiment(staging, output_parent):
    assert_available(output_parent)
    output_parent.mkdir(parents=True, exist_ok=True)
    destination = output_parent / staging.name
    temporary = output_parent / ("." + staging.name + ".tmp")
    if destination.exists() or temporary.exists():
        raise FileExistsError(destination)
    try:
        shutil.copytree(staging, temporary)
        for source in staging.rglob("*"):
            if source.is_file() and sha256(source) != sha256(temporary / source.relative_to(staging)):
                raise RuntimeError(f"SHA-256 divergente na cópia do experimento: {source}")
        os.replace(temporary, destination)
        shutil.rmtree(staging)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def run_probe(max_pages=80, delay_seconds=1.0, output_parent=DEFAULT_OUTPUT,
              sort="default", start_offset=0):
    if max_pages < 1 or delay_seconds < 0 or start_offset < 0 or start_offset % 10:
        raise ValueError("max_pages deve ser positivo, delay_seconds não negativo e offset múltiplo de 10")
    if sort not in {"default", "recent"}:
        raise ValueError(f"Sort não suportado pelo probe: {sort}")
    assert_available(CANONICAL)
    assert_available(output_parent)
    if not CANONICAL.is_file():
        raise FileNotFoundError(f"Base RSS canônica ausente: {CANONICAL}")
    assert_local((STAGING_ROOT,), output_parent)
    before_hash = sha256(CANONICAL)
    rss_ids = set(pd.read_parquet(CANONICAL, columns=["id_review"])["id_review"].astype(str))
    run_name = "inter_web_" + sort + f"_offset{start_offset}_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:6]
    staging = STAGING_ROOT / run_name
    (staging / "pages").mkdir(parents=True, exist_ok=False)
    requests = []
    unique = {}
    total_rows = 0
    first_url = FIRST_URL + ("&sort=recent" if sort == "recent" else "")
    if start_offset:
        first_url += f"&offset={start_offset}"
    next_url = first_url
    requested_urls = set()
    previous_ids = []
    stop_reason = "page_limit"
    started_at = timestamp()
    error = None
    try:
        for page in range(1, max_pages + 1):
            if next_url in requested_urls:
                stop_reason = "repeated_next_url"
                break
            requested_urls.add(next_url)
            entry = {"page": page, "url": next_url, "requested_at": timestamp()}
            try:
                request = Request(next_url, headers=HEADERS)
                with urlopen(request, timeout=30) as response:
                    entry["http_status"] = response.status
                    entry["content_type"] = response.headers.get("Content-Type")
                    body = response.read(10_000_001)
                if len(body) > 10_000_000:
                    raise ValueError("Resposta maior que 10 MB")
            except HTTPError as exc:
                entry["http_status"] = exc.code
                entry["error"] = str(exc.reason)
                entry["content_type"] = exc.headers.get("Content-Type")
                body = exc.read(10_000_001)
                stop_reason = f"http_{exc.code}"
                error = f"HTTP {exc.code}: {exc.reason}"
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                entry["error"] = str(exc)
                body = b""
                stop_reason = "request_error"
            if body:
                suffix = ".json" if entry.get("http_status") == 200 else ".body"
                page_path = staging / "pages" / f"{page:04d}{suffix}"
                page_path.write_bytes(body)
                entry["body_file"] = str(page_path.relative_to(staging))
                entry["body_sha256"] = sha256(page_path)
                entry["body_bytes"] = len(body)
            entry["received_at"] = timestamp()
            requests.append(entry)
            if stop_reason.startswith("http_") or stop_reason == "request_error":
                break
            try:
                payload = json.loads(body)
                page_reviews = extract_reviews(payload)
                current_ids = [review["id_review"] for review in page_reviews]
                entry["rows"] = len(page_reviews)
                entry["ids"] = current_ids
                total_rows += len(page_reviews)
                if repeated_page(current_ids, previous_ids):
                    stop_reason = "repeated_page_ids"
                    break
                previous_ids = current_ids
                for review in page_reviews:
                    unique.setdefault(review["id_review"], review)
                if not page_reviews:
                    stop_reason = "empty_with_next" if payload.get("next") else "source_end"
                    break
                if not payload.get("next"):
                    stop_reason = "source_end"
                    break
                next_url = normalize_next(payload["next"], sort=sort)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                error = str(exc)
                stop_reason = "invalid_response"
                break
            if page < max_pages:
                time.sleep(delay_seconds)
        after_hash = sha256(CANONICAL)
        if after_hash != before_hash:
            raise RuntimeError("A base RSS mudou durante o probe; comparação requer revisão")
        dates = [datetime.fromisoformat(x["web_date"].replace("Z", "+00:00")) for x in unique.values()]
        web_ids = set(unique)
        summary = {
            "app_id": APP_ID, "storefront": STOREFRONT, "source": "app_store_web_proxy",
            "sort": sort, "start_offset": start_offset, "first_url": first_url,
            "started_at": started_at, "finished_at": timestamp(),
            "max_pages": max_pages, "delay_seconds": delay_seconds,
            "pages_requested": len(requests), "pages_http_200": sum(x.get("http_status") == 200 for x in requests),
            "last_http_status": requests[-1].get("http_status") if requests else None,
            "stop_reason": stop_reason, "error": error,
            "rows": total_rows, "unique_reviews": len(web_ids), "duplicate_rows": total_rows - len(web_ids),
            "web_date_oldest": min(dates).isoformat() if dates else None,
            "web_date_newest": max(dates).isoformat() if dates else None,
            "reviews_by_year": dict(sorted(Counter(str(x.year) for x in dates).items())),
            "control_id_found": CONTROL_ID in web_ids,
            "rss_base_sha256_before": before_hash, "rss_base_sha256_after": after_hash,
            "rss_base_ids": len(rss_ids), "ids_in_both": len(web_ids & rss_ids),
            "ids_only_web": len(web_ids - rss_ids), "ids_only_rss": len(rss_ids - web_ids),
        }
        save_json(staging / "requests.json", requests)
        save_json(staging / "reviews.json", unique)
        save_json(staging / "summary.json", summary)
        return publish_experiment(staging, Path(output_parent))
    except BaseException:
        # Keep the local partial run for inspection; it cannot become a canonical base.
        raise


def compare_runs(first, second):
    first = Path(first)
    second = Path(second)
    a = json.loads((first / "reviews.json").read_text(encoding="utf-8"))
    b = json.loads((second / "reviews.json").read_text(encoding="utf-8"))
    ra = json.loads((first / "requests.json").read_text(encoding="utf-8"))
    rb = json.loads((second / "requests.json").read_text(encoding="utf-8"))
    sa = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    sb = json.loads((second / "summary.json").read_text(encoding="utf-8"))
    if (sa.get("sort", "default"), sa.get("start_offset", 0)) != (sb.get("sort", "default"), sb.get("start_offset", 0)):
        raise ValueError("Rodadas com sort ou offset inicial diferentes não são comparáveis por página")
    aa, bb = set(a), set(b)
    pages_a = [x.get("ids", []) for x in ra if x.get("http_status") == 200]
    pages_b = [x.get("ids", []) for x in rb if x.get("http_status") == 200]
    bodies_a = [x for x in ra if x.get("http_status") == 200]
    bodies_b = [x for x in rb if x.get("http_status") == 200]
    common_pages = min(len(pages_a), len(pages_b))
    comparable_a = set().union(*(set(x) for x in pages_a[:common_pages])) if common_pages else set()
    comparable_b = set().union(*(set(x) for x in pages_b[:common_pages])) if common_pages else set()
    next_differences = []
    body_differences = []
    for index in range(common_pages):
        left, right = bodies_a[index], bodies_b[index]
        if left.get("body_sha256") != right.get("body_sha256"):
            body_differences.append(index + 1)
        if left.get("body_file") and right.get("body_file"):
            left_next = json.loads((first / left["body_file"]).read_bytes()).get("next")
            right_next = json.loads((second / right["body_file"]).read_bytes()).get("next")
            if left_next != right_next:
                next_differences.append(index + 1)
    return {
        "first_run": str(first), "second_run": str(second),
        "first_started_at": sa["started_at"], "second_started_at": sb["started_at"],
        "first_stop_reason": sa["stop_reason"], "second_stop_reason": sb["stop_reason"],
        "first_unique": len(aa), "second_unique": len(bb),
        "shared_ids": len(aa & bb),
        "first_only_ids": sorted(aa - bb), "second_only_ids": sorted(bb - aa),
        "changed_shared_ids": sorted(k for k in aa & bb if a[k] != b[k]),
        "comparable_pages": common_pages,
        "pages_with_different_ids_or_order": [i + 1 for i in range(common_pages) if pages_a[i] != pages_b[i]],
        "pages_with_different_raw_body": body_differences,
        "pages_with_different_next": next_differences,
        "first_only_ids_on_comparable_pages": sorted(comparable_a - comparable_b),
        "second_only_ids_on_comparable_pages": sorted(comparable_b - comparable_a),
        "first_page_ids_first_run": ra[0].get("ids", []) if ra else [],
        "first_page_ids_second_run": rb[0].get("ids", []) if rb else [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--sort", choices=("default", "recent"), default="default")
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--compare", nargs=2, metavar=("FIRST", "SECOND"))
    args = parser.parse_args()
    if args.compare:
        print(json.dumps(compare_runs(*args.compare), ensure_ascii=False, indent=2))
        return
    result = run_probe(args.max_pages, args.delay_seconds, sort=args.sort,
                       start_offset=args.start_offset)
    print(result)
    print((result / "summary.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
