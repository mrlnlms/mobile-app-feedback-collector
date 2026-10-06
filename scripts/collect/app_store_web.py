"""Collect App Store Web reviews with an auditable, resumable page journal.

The Web archive is independent of the RSS base. Run from the repository root:
    venv/bin/python -m scripts.collect.app_store_web --bank inter --from-run PATH
    venv/bin/python -m scripts.collect.app_store_web --bank inter --collect
    venv/bin/python -m scripts.collect.app_store_web --bank inter --collect --resume-from PATH
"""

import argparse
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import pandas as pd
import yaml

from scripts.collect.storage import assert_available, assert_local, save_json, sha256


HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Accept-Language": "pt-BR"}
STAGING_ROOT = Path(".runtime/app_store/web")
RUNS_ROOT = Path("data/runs/app_store/web")
RAW_ROOT = Path("data/raw/app_store")
SNAPSHOT_ROOT = Path("data/runs/app_store/snapshots_web")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp sem offset: {value}")
    return parsed.astimezone(timezone.utc)


def first_url(app_id, storefront):
    return f"https://apps.apple.com/api/apps/v1/catalog/{storefront}/apps/{app_id}/reviews?platform=web&l=pt-BR&sort=recent"


def next_url(next_path, app_id, storefront):
    expected_path = f"/v1/catalog/{storefront}/apps/{app_id}/reviews"
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != expected_path:
        raise ValueError(f"Next fora da rota esperada: {next_path}")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key not in {"l", "offset", "platform", "sort"} for key, _ in pairs):
        raise ValueError(f"Parâmetro inesperado em next: {next_path}")
    if any(key == "sort" and value != "recent" for key, value in pairs):
        raise ValueError(f"Next mudou a ordenação: {next_path}")
    if any(key == "platform" and value != "web" for key, value in pairs):
        raise ValueError(f"Next mudou a plataforma: {next_path}")
    query = [(key, value) for key, value in pairs if key not in {"sort", "platform"}]
    query.extend([("platform", "web"), ("sort", "recent")])
    return urlunsplit(("https", "apps.apple.com", "/api/apps" + expected_path, urlencode(query), ""))


def offset_from_url(url):
    values = parse_qs(urlsplit(url).query).get("offset", ["0"])
    if len(values) != 1 or not values[0].isdigit():
        raise ValueError(f"Offset inválido: {url}")
    return int(values[0])


def extract_reviews(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Resposta Web sem lista data")
    result = []
    for row in payload["data"]:
        if not isinstance(row, dict) or row.get("type") != "user-reviews":
            raise ValueError("Item Web inesperado")
        attrs = row.get("attributes")
        if not isinstance(attrs, dict):
            raise ValueError("Review Web sem attributes")
        review_id, body, raw_date = row.get("id"), attrs.get("review"), attrs.get("date")
        if not isinstance(review_id, str) or not review_id or not isinstance(body, str) or not body:
            raise ValueError("Review Web sem ID ou texto")
        if not isinstance(raw_date, str):
            raise ValueError(f"Review Web {review_id} sem date")
        parse_utc(raw_date)
        result.append({
            "id_review": review_id, "web_date_raw": raw_date,
            "web_date_utc": parse_utc(raw_date).isoformat(),
            "web_texto_avaliacao": body, "web_titulo": attrs.get("title"),
            "web_nota": attrs.get("rating"), "web_usuario": attrs.get("userName"),
            "web_is_edited": attrs.get("isEdited"),
        })
    return result


def inspect_page(payload, offset, seen_ids, previous_oldest, app_id, storefront, cutoff):
    reviews = extract_reviews(payload)
    ids = [row["id_review"] for row in reviews]
    dates = [parse_utc(row["web_date_raw"]) for row in reviews]
    duplicates = sorted({review_id for review_id in ids if review_id in seen_ids or ids.count(review_id) > 1})
    anomalies = []
    if duplicates:
        anomalies.append("duplicate_ids")
    if any(a < b for a, b in zip(dates, dates[1:])):
        anomalies.append("date_increase_within_page")
    if dates and previous_oldest and dates[0] > parse_utc(previous_oldest):
        anomalies.append("date_increase_across_pages")
    next_raw = payload.get("next")
    following = next_url(next_raw, app_id, storefront) if next_raw else None
    next_offset = offset_from_url(following) if following else None
    if following and next_offset != offset + len(reviews):
        anomalies.append("next_offset_jump")
    if following and not reviews:
        anomalies.append("empty_with_next")
    if following and reviews and len(reviews) != 10:
        anomalies.append("short_page_with_next")
    return {
        "offset": offset, "review_count": len(reviews), "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None, "date_newest": max(dates).isoformat() if dates else None,
        "date_oldest": min(dates).isoformat() if dates else None,
        "duplicate_ids": duplicates, "next_present": bool(next_raw), "next_raw": next_raw,
        "next_url": following, "next_offset": next_offset,
        "before_cutoff": any(d.date() < cutoff for d in dates) if cutoff else False,
        "anomalies": anomalies, "ids": ids,
    }


def replay_run(run, app_id, storefront, cutoff=None):
    """Verify every raw SHA and confirmed page before materialization or resumption."""
    run = Path(run)
    journal = run / "pages.jsonl"
    if not journal.is_file():
        raise FileNotFoundError(journal)
    entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    seen = set()
    expected = first_url(app_id, storefront)
    previous_oldest = None
    last_offset = None
    confirmed = []
    for entry in entries:
        if entry.get("body_file"):
            body = run / entry["body_file"]
            if not body.is_file() or sha256(body) != entry.get("body_sha256"):
                raise ValueError(f"Payload bruto ausente ou hash divergente: {body}")
        if entry.get("http_status") != 200:
            continue
        if entry.get("url") != expected or offset_from_url(expected) != entry.get("offset"):
            raise ValueError(f"Sequência Web inconsistente no offset {entry.get('offset')}")
        payload = json.loads((run / entry["body_file"]).read_bytes())
        check = inspect_page(payload, entry["offset"], seen, previous_oldest, app_id, storefront, cutoff)
        if check["anomalies"] or check["ids"] != entry.get("ids") or check["review_count"] != entry.get("review_count"):
            raise ValueError(f"Página Web inconsistente no offset {entry['offset']}")
        if not check["ids"]:
            raise ValueError("Página Web 200 vazia não pode ser materializada sem revisão")
        confirmed.append(entry)
        seen.update(check["ids"])
        previous_oldest = check["date_oldest"]
        expected = check["next_url"]
        last_offset = entry["offset"]
    summary_file = run / "summary.json"
    if summary_file.is_file():
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        if str(summary.get("app_id")) != str(app_id) or summary.get("storefront") != storefront:
            raise ValueError("Run Web pertence a outro app ou storefront")
        if summary.get("unique_ids") != len(seen) or summary.get("pages_http_200") != len(confirmed):
            raise ValueError("Resumo Web diverge do journal verificado")
    return {"entries": entries, "confirmed": confirmed, "ids": seen,
            "next_url": expected, "last_offset": last_offset,
            "previous_oldest": previous_oldest}


def append_journal(path, entry):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def retry_after_seconds(value):
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return None


def collect_web(bank, app_id, storefront, cutoff=None, delay_seconds=15.0,
                max_pages=1000, max_429_retries=3, resume_from=None):
    """Fetch sequential pages, retaining raw bodies and a resumable confirmed journal."""
    if delay_seconds < 4 or max_pages < 1 or max_429_retries < 0:
        raise ValueError("Pausa mínima de 4 segundos e limites não negativos")
    if cutoff is not None and not isinstance(cutoff, date):
        raise ValueError("cutoff deve ser date ou None")
    archive_parent = RUNS_ROOT / bank
    assert_available(archive_parent)
    assert_local((STAGING_ROOT,), archive_parent)
    name = f"{bank}_recent_{datetime.now(timezone.utc):%Y%m%dT%H%M%S_%fZ}_{uuid.uuid4().hex[:6]}"
    staging = STAGING_ROOT / bank / name
    if resume_from:
        prior = replay_run(resume_from, app_id, storefront, cutoff)
        if prior["next_url"] is None:
            raise ValueError("Run anterior chegou ao fim natural")
        if Path(resume_from).resolve().is_relative_to(STAGING_ROOT.resolve()):
            staging = Path(resume_from)
        else:
            shutil.copytree(resume_from, staging)
    else:
        (staging / "pages").mkdir(parents=True, exist_ok=False)
        prior = {"entries": [], "confirmed": [], "ids": set(), "next_url": first_url(app_id, storefront),
                 "last_offset": None, "previous_oldest": None}
    journal = staging / "pages.jsonl"
    current = prior["next_url"]
    seen = set(prior["ids"])
    previous_oldest = prior["previous_oldest"]
    confirmed_offsets = {entry["offset"] for entry in prior["confirmed"]}
    attempted = len(prior["entries"])
    confirmed_count = len(prior["confirmed"])
    rate_limits = 0
    retry_streak = 0
    new_pages = 0
    stop_reason = "page_limit"
    started_at = utc_now()
    while new_pages < max_pages:
        offset = offset_from_url(current)
        if offset in confirmed_offsets:
            stop_reason = "repeated_offset"
            break
        entry = {"page": attempted + 1, "offset": offset, "url": current,
                 "requested_at": utc_now(), "retry_number": retry_streak}
        body = b""
        try:
            with urlopen(Request(current, headers=HEADERS), timeout=30) as response:
                entry["http_status"] = response.status
                entry["content_type"] = response.headers.get("Content-Type")
                body = response.read(10_000_001)
            if len(body) > 10_000_000:
                raise ValueError("Resposta Web maior que 10 MB")
        except HTTPError as exc:
            entry.update({"http_status": exc.code, "content_type": exc.headers.get("Content-Type"),
                          "retry_after_header": exc.headers.get("Retry-After"), "error": str(exc.reason)})
            try:
                body = exc.read(10_000_001)
            finally:
                exc.close()
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            entry["error"] = str(exc)
            stop_reason = "request_error"
        attempted += 1
        if body:
            suffix = ".json" if entry.get("http_status") == 200 else ".body"
            path = staging / "pages" / f"{attempted:04d}_{uuid.uuid4().hex[:8]}{suffix}"
            with path.open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            entry.update({"body_file": str(path.relative_to(staging)),
                          "body_sha256": sha256(path), "body_bytes": len(body)})
        entry["received_at"] = utc_now()
        if entry.get("http_status") == 429:
            rate_limits += 1
            retry_streak += 1
            header_wait = retry_after_seconds(entry.get("retry_after_header"))
            wait = max(delay_seconds, header_wait) if header_wait is not None else max(delay_seconds, min(60 * 2 ** (retry_streak - 1), 600))
            entry.update({"backoff_source": "retry_after" if header_wait is not None else "exponential",
                          "suggested_backoff_seconds": wait, "will_retry": retry_streak <= max_429_retries,
                          "backoff_seconds": wait if retry_streak <= max_429_retries else 0.0})
        if entry.get("http_status") != 200 or stop_reason == "request_error":
            append_journal(journal, entry)
            save_json(staging / "progress.json", {"updated_at": utc_now(), "last_confirmed_offset": max(confirmed_offsets, default=None),
                                                  "next_url": current, "next_offset": offset, "unique_ids": len(seen)})
            if entry.get("http_status") == 429 and entry["will_retry"]:
                time.sleep(entry["backoff_seconds"])
                continue
            stop_reason = "rate_limited_paused" if entry.get("http_status") == 429 else f"http_{entry.get('http_status')}" if entry.get("http_status") else "request_error"
            break
        try:
            payload = json.loads(body)
            check = inspect_page(payload, offset, seen, previous_oldest, app_id, storefront, cutoff)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            entry.update({"error": str(exc), "anomalies": ["invalid_response"]})
            append_journal(journal, entry)
            stop_reason = "invalid_response"
            break
        entry.update(check)
        append_journal(journal, entry)
        if check["anomalies"]:
            stop_reason = "continuity_uncertain"
            break
        confirmed_count += 1
        new_pages += 1
        confirmed_offsets.add(offset)
        seen.update(check["ids"])
        previous_oldest = check["date_oldest"]
        current = check["next_url"]
        retry_streak = 0
        save_json(staging / "progress.json", {"updated_at": utc_now(), "last_confirmed_offset": offset,
                                              "next_url": current, "next_offset": offset_from_url(current) if current else None,
                                              "unique_ids": len(seen)})
        if check["before_cutoff"]:
            stop_reason = "cutoff_reached"
            break
        if not current:
            stop_reason = "source_end"
            break
        if new_pages < max_pages:
            time.sleep(delay_seconds)
    summary = {"app_id": str(app_id), "storefront": storefront, "sort": "recent", "bank": bank,
               "started_at": started_at, "finished_at": utc_now(), "resume_source": str(resume_from) if resume_from else None,
               "delay_seconds": delay_seconds, "pages_attempted": attempted, "pages_http_200": confirmed_count,
               "rate_limit_attempts_this_run": rate_limits, "last_confirmed_offset": max(confirmed_offsets, default=None),
               "next_offset": offset_from_url(current) if current else None, "unique_ids": len(seen),
               "stop_reason": stop_reason, "cutoff": cutoff.isoformat() if cutoff else None}
    save_json(staging / "summary.json", summary)
    replay_run(staging, app_id, storefront, cutoff)
    archive_parent.mkdir(parents=True, exist_ok=True)
    destination = archive_parent / staging.name
    temporary = archive_parent / ("." + staging.name + ".tmp")
    if destination.exists() or temporary.exists():
        raise FileExistsError(destination)
    shutil.copytree(staging, temporary)
    for source in staging.rglob("*"):
        if source.is_file() and sha256(source) != sha256(temporary / source.relative_to(staging)):
            raise RuntimeError(f"Hash divergente na cópia Web: {source}")
    os.replace(temporary, destination)
    shutil.rmtree(staging)
    return destination


def build_web_frame(run, app_id, storefront):
    state = replay_run(run, app_id, storefront)
    if not state["confirmed"]:
        raise ValueError("Nenhuma página Web confirmada para materialização")
    rows = []
    for entry in state["confirmed"]:
        payload = json.loads((Path(run) / entry["body_file"]).read_bytes())
        for review in extract_reviews(payload):
            rows.append({**review, "apple_app_id": str(app_id), "pais": storefront,
                         "web_sort": "recent", "web_offset": entry["offset"],
                         "web_requested_at": entry["requested_at"], "web_run": str(run),
                         "web_body_file": entry["body_file"], "web_body_sha256": entry["body_sha256"]})
    frame = pd.DataFrame(rows)
    if len(frame) != len(state["ids"]) or frame.id_review.duplicated().any():
        raise ValueError("IDs Web duplicados ou divergentes do journal")
    frame["web_date_utc"] = pd.to_datetime(frame["web_date_utc"], utc=True)
    frame["web_requested_at"] = pd.to_datetime(frame["web_requested_at"], utc=True)
    if not frame.web_nota.isin([1, 2, 3, 4, 5]).all():
        raise ValueError("Notas Web inválidas")
    return frame


def publish_frame(frame, output, staging, snapshots):
    """Stage locally, verify the copied Parquet, snapshot any previous base, replace atomically."""
    output, staging, snapshots = Path(output), Path(staging), Path(snapshots)
    assert_available(output)
    assert_available(snapshots)
    assert_local((staging,), output.parent)
    if frame.empty or frame.id_review.isna().any() or frame.id_review.duplicated().any():
        raise ValueError("Base vazia ou com IDs inválidos")
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / "prepared.parquet"
    frame.to_parquet(staged, index=False)
    readback = pd.read_parquet(staged)
    if len(readback) != len(frame) or readback.id_review.duplicated().any():
        raise RuntimeError("Parquet preparado diverge da base validada")
    final_hash = sha256(staged)
    old_hash = sha256(output) if output.is_file() else None
    if old_hash:
        previous = pd.read_parquet(output)
        if list(previous.columns) == list(readback.columns):
            old_sorted = previous.sort_values("id_review").reset_index(drop=True)
            new_sorted = readback.sort_values("id_review").reset_index(drop=True)
            if old_sorted.equals(new_sorted):
                if sha256(output) != old_hash:
                    raise RuntimeError("Destino mudou durante a verificação")
                staged.unlink()
                return {"path": str(output), "sha256": old_hash, "reviews": len(frame),
                        "previous_sha256": old_hash, "snapshot": None, "unchanged": True}
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=output.stem + ".", suffix=".tmp.parquet", dir=output.parent)
    os.close(descriptor)
    temporary = Path(name)
    snapshot = None
    try:
        shutil.copy2(staged, temporary)
        if sha256(temporary) != final_hash:
            raise RuntimeError("Hash divergente na cópia do Parquet")
        if (sha256(output) if output.is_file() else None) != old_hash:
            raise RuntimeError("Destino mudou antes da publicação")
        if old_hash:
            snapshots.mkdir(parents=True, exist_ok=True)
            snapshot = snapshots / f"{output.stem}_before_{datetime.now(timezone.utc):%Y%m%dT%H%M%S_%fZ}.parquet"
            shutil.copy2(output, snapshot)
            if sha256(snapshot) != old_hash:
                raise RuntimeError("Hash divergente no snapshot")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    if sha256(output) != final_hash:
        raise RuntimeError("Hash final do Parquet divergente")
    return {"path": str(output), "sha256": final_hash, "reviews": len(frame),
            "previous_sha256": old_hash, "snapshot": str(snapshot) if snapshot else None}


def materialize_web(bank, app_id, storefront, run):
    summary_file = Path(run) / "summary.json"
    if not summary_file.is_file():
        raise FileNotFoundError(summary_file)
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    if summary.get("stop_reason") not in {"pre_2025_reached", "cutoff_reached", "source_end"}:
        raise ValueError("Run Web ainda não alcançou a fronteira ou o fim natural")
    fresh = build_web_frame(run, app_id, storefront)
    if summary["stop_reason"] == "source_end":
        if replay_run(run, app_id, storefront)["next_url"] is not None:
            raise ValueError("Run Web indica fim, mas a última página aponta para outra")
    else:
        boundary = date(2025, 1, 1) if summary["stop_reason"] == "pre_2025_reached" else date.fromisoformat(summary["cutoff"])
        if fresh.web_date_utc.min().date() >= boundary:
            raise ValueError("Run Web não atravessou a data de corte declarada")
    output = RAW_ROOT / bank / "reviews_web.parquet"
    if output.is_file():
        previous = pd.read_parquet(output)
        if previous.id_review.isna().any() or previous.id_review.duplicated().any():
            raise ValueError("Acervo Web anterior contém IDs inválidos")
        if set(previous.columns) != set(fresh.columns):
            raise ValueError("Schema Web anterior incompatível")
        new_ids = len(set(fresh.id_review) - set(previous.id_review))
        preserved_ids = len(set(previous.id_review) - set(fresh.id_review))
        frame = pd.concat([fresh, previous], ignore_index=True).drop_duplicates("id_review", keep="first")
    else:
        new_ids = len(fresh)
        preserved_ids = 0
        frame = fresh
    frame = frame.sort_values("id_review").reset_index(drop=True)
    receipt = publish_frame(frame, output, STAGING_ROOT / bank / "publication", SNAPSHOT_ROOT / bank)
    receipt.update({"app_id": str(app_id), "storefront": storefront, "source_run": str(run),
                    "source_journal_sha256": sha256(Path(run) / "pages.jsonl"), "published_at": utc_now(),
                    "run_reviews": len(fresh), "new_ids": new_ids, "older_ids_preserved": preserved_ids,
                    "date_min_utc": frame.web_date_utc.min().isoformat(), "date_max_utc": frame.web_date_utc.max().isoformat()})
    save_json(RUNS_ROOT / bank / "web_archive_state.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--collect", action="store_true")
    action.add_argument("--from-run", type=Path)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--delay-seconds", type=float, default=15.0)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-429-retries", type=int, default=3)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.bank not in config["banks"]:
        parser.error(f"Banco desconhecido: {args.bank}")
    app_id = config["banks"][args.bank]["apple_app_id"]
    storefront = config["app_store"]["country"]
    if args.from_run:
        if args.resume_from:
            parser.error("--resume-from requer --collect")
        print(json.dumps(materialize_web(args.bank, app_id, storefront, args.from_run), indent=2, ensure_ascii=False))
        return
    cutoff = date.fromisoformat(config["banks"][args.bank]["start_date"])
    run = collect_web(args.bank, app_id, storefront, cutoff, args.delay_seconds,
                      args.max_pages, args.max_429_retries, args.resume_from)
    print(run)
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if summary["stop_reason"] in {"cutoff_reached", "source_end"}:
        print(json.dumps(materialize_web(args.bank, app_id, storefront, run), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
