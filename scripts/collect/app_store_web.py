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


def url_at_offset(app_id, storefront, offset):
    return next_url(f"/v1/catalog/{storefront}/apps/{app_id}/reviews?l=pt-BR&offset={offset}",
                    app_id, storefront)


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


def inspect_page(payload, offset, seen_ids, previous_oldest, app_id, storefront, cutoff,
                 previous_ids=(), accept_overlap=True):
    reviews = extract_reviews(payload)
    ids = [row["id_review"] for row in reviews]
    dates = [parse_utc(row["web_date_raw"]) for row in reviews]
    duplicates = sorted({review_id for review_id in ids if review_id in seen_ids or ids.count(review_id) > 1})
    overlap_count = 0
    if accept_overlap and duplicates and len(ids) == len(set(ids)) and previous_ids and previous_oldest:
        for count in range(min(len(ids) - 1, len(previous_ids)), 0, -1):
            if (ids[:count] == list(previous_ids)[-count:]
                    and set(duplicates) == set(ids[:count])
                    and not any(review_id in seen_ids for review_id in ids[count:])
                    and dates[count - 1] == parse_utc(previous_oldest)):
                overlap_count = count
                break
    anomalies = []
    if duplicates and not overlap_count:
        anomalies.append("duplicate_ids")
    if any(a < b for a, b in zip(dates, dates[1:])):
        anomalies.append("date_increase_within_page")
    if dates and previous_oldest and dates[overlap_count] > parse_utc(previous_oldest):
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
        "anomalies": anomalies, "ids": ids, "overlap_ids": ids[:overlap_count],
    }


def inspect_recovery_page(payload, offset, app_id, storefront, archived_rows,
                          archived_positions, terminal_id, terminal_oldest,
                          previous_oldest, recovery_seen, last_archived_position,
                          tail_seen, new_started):
    """Verify a direct-offset walk across an apparent source end."""
    reviews = extract_reviews(payload)
    if len(reviews) != 10:
        raise ValueError("Página de recuperação deve conter dez reviews")
    ids = [row["id_review"] for row in reviews]
    if len(set(ids)) != len(ids) or set(ids) & recovery_seen:
        raise ValueError("ID repetido dentro da travessia de recuperação")
    dates = [parse_utc(row["web_date_raw"]) for row in reviews]
    if any(left < right for left, right in zip(dates, dates[1:])):
        raise ValueError("Datas crescentes na página de recuperação")
    if previous_oldest and dates[0] > parse_utc(previous_oldest):
        raise ValueError("Datas crescentes entre páginas de recuperação")
    following = next_url(payload.get("next"), app_id, storefront) if payload.get("next") else None
    if not following or offset_from_url(following) != offset + len(reviews):
        raise ValueError("Recuperação sem próximo offset sequencial")
    existing = []
    novel = []
    for row, review_date in zip(reviews, dates):
        review_id = row["id_review"]
        if review_id in archived_positions:
            if new_started:
                raise ValueError("ID arquivado voltou depois de IDs novos")
            position = archived_positions[review_id]
            if position <= last_archived_position:
                raise ValueError("Posições arquivadas retrocederam na recuperação")
            if row != archived_rows[review_id]:
                raise ValueError(f"Campos do review arquivado divergem: {review_id}")
            existing.append(review_id)
            last_archived_position = position
            if review_id == terminal_id:
                tail_seen = True
        else:
            if not tail_seen:
                raise ValueError("Recuperação chegou a IDs novos antes do último ID arquivado")
            if review_date >= parse_utc(terminal_oldest):
                raise ValueError("ID novo não é mais antigo que a extremidade arquivada")
            novel.append(review_id)
            new_started = True
    return {
        "offset": offset, "review_count": len(ids), "first_id": ids[0],
        "last_id": ids[-1], "date_newest": dates[0].isoformat(),
        "date_oldest": dates[-1].isoformat(), "duplicate_ids": existing,
        "next_present": True, "next_raw": payload["next"],
        "next_url": following, "next_offset": offset_from_url(following),
        "anomalies": [], "ids": ids, "overlap_ids": [],
        "recovery_existing_ids": existing, "recovery_new_ids": novel,
        "last_archived_position": last_archived_position,
        "tail_seen": tail_seen, "new_started": new_started,
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
    previous_ids = []
    last_offset = None
    confirmed = []
    historical_rows = {}
    historical_positions = {}
    recovery_active = False
    recovery_seen = set()
    recovery_previous_oldest = None
    recovery_last_position = -1
    recovery_tail_seen = False
    recovery_new_started = False
    pending_anomaly_offset = None
    for entry in entries:
        if entry.get("body_file"):
            body = run / entry["body_file"]
            if not body.is_file() or sha256(body) != entry.get("body_sha256"):
                raise ValueError(f"Payload bruto ausente ou hash divergente: {body}")
        if entry.get("http_status") != 200:
            if entry.get("recovery_attempt") and (not recovery_active or entry.get("url") != expected
                                                   or entry.get("offset") != offset_from_url(expected)):
                raise ValueError("Tentativa de recuperação fora da sequência")
            continue
        if entry.get("recovery_page"):
            if not recovery_active:
                if not confirmed or not previous_ids or not previous_oldest:
                    raise ValueError("Recuperação requer página anterior confirmada")
                if expected is None:
                    if entry.get("recovery_after_anomaly"):
                        raise ValueError("Recuperação de anomalia requer next pendente")
                    expected = url_at_offset(app_id, storefront, last_offset + len(previous_ids))
                elif not (entry.get("recovery_after_anomaly")
                          and pending_anomaly_offset == entry.get("offset")):
                    raise ValueError("Recuperação com next requer anomalia no offset pendente")
                bridge_rows = historical_rows.copy()
                bridge_positions = historical_positions.copy()
                terminal_id = previous_ids[-1]
                terminal_oldest = previous_oldest
                recovery_active = True
                recovery_seen = set()
                recovery_previous_oldest = None
                recovery_last_position = -1
                recovery_tail_seen = False
                recovery_new_started = False
                pending_anomaly_offset = None
            if entry.get("url") != expected or entry.get("offset") != offset_from_url(expected):
                raise ValueError(f"Sequência de recuperação inconsistente no offset {entry.get('offset')}")
            payload = json.loads((run / entry["body_file"]).read_bytes())
            check = inspect_recovery_page(
                payload, entry["offset"], app_id, storefront, bridge_rows,
                bridge_positions, terminal_id, terminal_oldest,
                recovery_previous_oldest, recovery_seen, recovery_last_position,
                recovery_tail_seen, recovery_new_started)
            if (entry.get("ids") != check["ids"]
                    or entry.get("review_count") != check["review_count"]
                    or entry.get("recovery_existing_ids") != check["recovery_existing_ids"]
                    or entry.get("recovery_new_ids") != check["recovery_new_ids"]
                    or entry.get("next_url") != check["next_url"]):
                raise ValueError(f"Página de recuperação divergente no offset {entry['offset']}")
            recovery_seen.update(check["ids"])
            recovery_previous_oldest = check["date_oldest"]
            recovery_last_position = check["last_archived_position"]
            recovery_tail_seen = check["tail_seen"]
            recovery_new_started = check["new_started"]
            confirmed.append(entry)
            for row in extract_reviews(payload):
                review_id = row["id_review"]
                if review_id not in historical_rows:
                    historical_positions[review_id] = len(historical_positions)
                    historical_rows[review_id] = row
            seen.update(check["ids"])
            previous_oldest = check["date_oldest"]
            previous_ids = check["ids"]
            expected = check["next_url"]
            last_offset = entry["offset"]
            pending_anomaly_offset = None
            continue
        if recovery_active:
            if not recovery_tail_seen or not recovery_new_started:
                raise ValueError("Recuperação não atravessou o último ID arquivado")
            recovery_active = False
        if entry.get("url") != expected or offset_from_url(expected) != entry.get("offset"):
            raise ValueError(f"Sequência Web inconsistente no offset {entry.get('offset')}")
        payload = json.loads((run / entry["body_file"]).read_bytes())
        recorded_anomalies = entry.get("anomalies") or []
        check = inspect_page(payload, entry["offset"], seen, previous_oldest, app_id,
                             storefront, cutoff, previous_ids,
                             accept_overlap=not recorded_anomalies)
        if (check["anomalies"] != recorded_anomalies
                or check["ids"] != entry.get("ids")
                or check["review_count"] != entry.get("review_count")
                or check["overlap_ids"] != entry.get("overlap_ids", [])):
            raise ValueError(f"Página Web inconsistente no offset {entry['offset']}")
        if recorded_anomalies:
            pending_anomaly_offset = entry["offset"]
            continue
        if not check["ids"]:
            raise ValueError("Página Web 200 vazia não pode ser materializada sem revisão")
        confirmed.append(entry)
        for row in extract_reviews(payload):
            review_id = row["id_review"]
            if review_id not in historical_rows:
                historical_positions[review_id] = len(historical_positions)
                historical_rows[review_id] = row
        seen.update(check["ids"])
        previous_oldest = check["date_oldest"]
        previous_ids = check["ids"]
        expected = check["next_url"]
        last_offset = entry["offset"]
        pending_anomaly_offset = None
    if recovery_active and (not recovery_tail_seen or not recovery_new_started):
        raise ValueError("Recuperação não atravessou o último ID arquivado")
    summary_file = run / "summary.json"
    if summary_file.is_file():
        summary = json.loads(summary_file.read_text(encoding="utf-8"))
        if str(summary.get("app_id")) != str(app_id) or summary.get("storefront") != storefront:
            raise ValueError("Run Web pertence a outro app ou storefront")
        if summary.get("unique_ids") != len(seen) or summary.get("pages_http_200") != len(confirmed):
            raise ValueError("Resumo Web diverge do journal verificado")
    return {"entries": entries, "confirmed": confirmed, "ids": seen,
            "next_url": expected, "last_offset": last_offset,
            "previous_oldest": previous_oldest, "previous_ids": previous_ids}


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


def is_review_resource_404(body):
    """Identify Apple's observed transient missing-reviews response."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return isinstance(errors, list) and any(
        isinstance(error, dict) and error.get("status") == "404"
        and error.get("code") == "40403"
        and "reviews" in str(error.get("detail", "")).lower()
        for error in errors)


def archive_web_staging(staging, archive_parent):
    """Copy a validated local run to the archive, checking every file hash."""
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


def _recover_preserved_pages(bank, app_id, storefront, cutoff, source_run, checkpoint_path, mode):
    """Import a verified repeated span after source end or a continuity pause."""
    source_run = Path(source_run)
    checkpoint_path = Path(checkpoint_path)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    expected_kind = ("app_store_web_source_end_recovery_probe" if mode == "source_end"
                     else "app_store_web_continuity_recovery_probe")
    if (checkpoint.get("kind") != expected_kind
            or checkpoint.get("version") != 1
            or checkpoint.get("bank") != bank
            or str(checkpoint.get("app_id")) != str(app_id)
            or checkpoint.get("storefront") != storefront):
        raise ValueError("Checkpoint de recuperação pertence a outro banco ou formato")
    if Path(checkpoint["source_run"]).resolve() != source_run.resolve():
        raise ValueError("Checkpoint aponta para outro source run")
    if sha256(source_run / "pages.jsonl") != checkpoint.get("source_journal_sha256"):
        raise ValueError("Hash divergente do source run")
    canonical = RAW_ROOT / bank / "reviews_web.parquet"
    assert_available(canonical)
    if (Path(checkpoint["canonical_web_parquet"]).resolve() != canonical.resolve()
            or not canonical.is_file()
            or sha256(canonical) != checkpoint.get("canonical_web_sha256")):
        raise ValueError("Base Web canônica diverge do checkpoint")
    prior = replay_run(source_run, app_id, storefront, cutoff)
    source_summary = json.loads((source_run / "summary.json").read_text(encoding="utf-8"))
    if not prior["confirmed"] or prior["last_offset"] != checkpoint.get("source_last_offset"):
        raise ValueError("Source run não terminou no checkpoint esperado")
    if mode == "source_end":
        if source_summary.get("stop_reason") != "source_end" or prior["next_url"] is not None:
            raise ValueError("Source run não terminou no source_end esperado")
    elif (source_summary.get("stop_reason") != "continuity_uncertain"
          or prior["next_url"] != url_at_offset(app_id, storefront,
                                                prior["last_offset"] + len(prior["previous_ids"]))
          or not prior["entries"]
          or prior["entries"][-1].get("http_status") != 200
          or not prior["entries"][-1].get("anomalies")
          or prior["entries"][-1].get("offset") != offset_from_url(prior["next_url"])):
        raise ValueError("Source run não terminou em anomalia no offset pendente")
    if cutoff and parse_utc(prior["previous_oldest"]).date() < cutoff:
        raise ValueError("Source run já atravessou o corte")
    pages = checkpoint.get("probe_pages")
    rate_limits = checkpoint.get("rate_limit_responses")
    if not isinstance(pages, list) or not pages or not isinstance(rate_limits, list):
        raise ValueError("Checkpoint sem páginas ou respostas de rate limit")
    expected_offsets = list(range(prior["last_offset"] + len(prior["previous_ids"]),
                                  checkpoint["last_probe_offset"] + 1, 10))
    if ([record.get("offset") for record in pages] != expected_offsets
            or checkpoint.get("direct_probe_first_offset") != expected_offsets[0]
            or checkpoint.get("next_offset") != expected_offsets[-1] + 10):
        raise ValueError("Offsets do checkpoint não formam sequência contínua")
    attempts = []
    for record, expected_status in ([(record, 200) for record in pages]
                                    + [(record, 429) for record in rate_limits]):
        folder = Path(record["probe_dir"])
        metadata = json.loads((folder / "probe.json").read_text(encoding="utf-8"))
        body = folder / metadata.get("body_file", "response.body")
        if (metadata.get("http_status") != expected_status
                or sha256(body) != metadata.get("body_sha256")
                or metadata.get("body_sha256") != record.get("body_sha256")):
            raise ValueError(f"Payload do probe com hash divergente: {folder}")
        offset = offset_from_url(metadata["url"])
        if (metadata["url"] != url_at_offset(app_id, storefront, offset)
                or (expected_status == 200 and (record.get("offset") != offset
                                                  or Path(record["body_file"]).resolve() != body.resolve()))):
            raise ValueError(f"URL ou corpo do probe divergente: {folder}")
        attempts.append((parse_utc(metadata["requested_at"]), metadata, body, expected_status))
    attempts.sort(key=lambda item: item[0])
    archive_parent = RUNS_ROOT / bank
    assert_available(archive_parent)
    assert_local((STAGING_ROOT,), archive_parent)
    name = f"{bank}_recent_recovered_{datetime.now(timezone.utc):%Y%m%dT%H%M%S_%fZ}_{uuid.uuid4().hex[:6]}"
    staging = STAGING_ROOT / bank / name
    shutil.copytree(source_run, staging)
    (staging / "summary.json").unlink()
    copied_checkpoint = staging / "recovery_checkpoint.json"
    shutil.copy2(checkpoint_path, copied_checkpoint)
    if sha256(copied_checkpoint) != sha256(checkpoint_path):
        raise RuntimeError("Hash divergente na cópia do checkpoint")
    journal = staging / "pages.jsonl"
    attempted = len(prior["entries"])
    original_ids = set(prior["ids"])
    imported_new_ids = []
    for _, metadata, body, status in attempts:
        attempted += 1
        offset = offset_from_url(metadata["url"])
        suffix = ".json" if status == 200 else ".body"
        target = staging / "pages" / f"{attempted:04d}_{uuid.uuid4().hex[:8]}{suffix}"
        shutil.copy2(body, target)
        if sha256(target) != metadata["body_sha256"]:
            raise RuntimeError(f"Hash divergente na cópia do probe: {body}")
        entry = {"page": attempted, "offset": offset, "url": metadata["url"],
                 "requested_at": metadata["requested_at"],
                 "received_at": metadata.get("received_at"),
                 "http_status": status, "content_type": metadata.get("content_type"),
                 "body_file": str(target.relative_to(staging)),
                 "body_sha256": metadata["body_sha256"], "body_bytes": target.stat().st_size,
                 "recovery_probe_dir": str(body.parent)}
        if status == 200:
            payload = json.loads(target.read_bytes())
            reviews = extract_reviews(payload)
            ids = [row["id_review"] for row in reviews]
            existing = [review_id for review_id in ids if review_id in original_ids]
            novel = [review_id for review_id in ids if review_id not in original_ids]
            imported_new_ids.extend(novel)
            following = next_url(payload.get("next"), app_id, storefront) if payload.get("next") else None
            entry.update({"recovery_page": True, "review_count": len(ids), "ids": ids,
                          "first_id": ids[0] if ids else None, "last_id": ids[-1] if ids else None,
                          "recovery_existing_ids": existing, "recovery_new_ids": novel,
                          "next_url": following, "next_offset": offset_from_url(following) if following else None})
            if mode == "continuity" and offset == expected_offsets[0]:
                entry["recovery_after_anomaly"] = True
        else:
            entry["recovery_attempt"] = True
        append_journal(journal, entry)
    state = replay_run(staging, app_id, storefront, cutoff)
    if (state["last_offset"] != checkpoint["last_probe_offset"]
            or state["next_url"] != checkpoint.get("next_url")
            or len(state["confirmed"]) != len(prior["confirmed"]) + len(pages)
            or imported_new_ids != checkpoint.get("new_review_ids")
            or set(state["ids"]) - original_ids != set(imported_new_ids)):
        raise ValueError("Run recuperado diverge do checkpoint")
    save_json(staging / "progress.json", {
        "updated_at": utc_now(), "last_confirmed_offset": state["last_offset"],
        "next_url": state["next_url"], "next_offset": offset_from_url(state["next_url"]),
        "unique_ids": len(state["ids"])})
    save_json(staging / "summary.json", {
        "app_id": str(app_id), "storefront": storefront, "sort": "recent", "bank": bank,
        "started_at": utc_now(), "finished_at": utc_now(), "resume_source": str(source_run),
        "recovery_mode": mode,
        "recovery_checkpoint": str(checkpoint_path),
        "recovery_checkpoint_sha256": sha256(copied_checkpoint),
        "pages_attempted": len(state["entries"]), "pages_http_200": len(state["confirmed"]),
        "recovery_imported_429": len(rate_limits),
        "recovery_imported_new_ids": len(imported_new_ids),
        "last_confirmed_offset": state["last_offset"],
        "next_offset": offset_from_url(state["next_url"]), "unique_ids": len(state["ids"]),
        "stop_reason": "page_limit", "cutoff": cutoff.isoformat() if cutoff else None})
    replay_run(staging, app_id, storefront, cutoff)
    if sha256(canonical) != checkpoint["canonical_web_sha256"]:
        raise RuntimeError("Base Web canônica mudou durante a recuperação")
    return archive_web_staging(staging, archive_parent)


def recover_source_end(bank, app_id, storefront, cutoff, source_run, checkpoint_path):
    """Import a verified direct-offset probe after a source-end response."""
    return _recover_preserved_pages(bank, app_id, storefront, cutoff, source_run,
                                    checkpoint_path, "source_end")


def recover_continuity(bank, app_id, storefront, cutoff, source_run, checkpoint_path):
    """Import a verified repeated span after a continuity pause."""
    return _recover_preserved_pages(bank, app_id, storefront, cutoff, source_run,
                                    checkpoint_path, "continuity")


def collect_web(bank, app_id, storefront, cutoff=None, delay_seconds=15.0,
                max_pages=1000, max_429_retries=3, resume_from=None, max_404_retries=3):
    """Fetch sequential pages, retaining raw bodies and a resumable confirmed journal."""
    if delay_seconds < 4 or max_pages < 1 or max_429_retries < 0 or max_404_retries < 0:
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
        (staging / "summary.json").unlink(missing_ok=True)
    else:
        (staging / "pages").mkdir(parents=True, exist_ok=False)
        prior = {"entries": [], "confirmed": [], "ids": set(), "next_url": first_url(app_id, storefront),
                 "last_offset": None, "previous_oldest": None, "previous_ids": []}
    journal = staging / "pages.jsonl"
    current = prior["next_url"]
    seen = set(prior["ids"])
    previous_oldest = prior["previous_oldest"]
    previous_ids = prior["previous_ids"]
    confirmed_offsets = {entry["offset"] for entry in prior["confirmed"]}
    attempted = len(prior["entries"])
    confirmed_count = len(prior["confirmed"])
    rate_limits = 0
    retry_streak = 0
    review_404_attempts = 0
    not_found_streak = 0
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
        if entry.get("http_status") == 404 and confirmed_count and is_review_resource_404(body):
            review_404_attempts += 1
            not_found_streak += 1
            wait = max(delay_seconds, min(60 * 2 ** (not_found_streak - 1), 600))
            entry.update({"review_resource_404": True,
                          "review_404_retry_number": not_found_streak,
                          "backoff_source": "review_404_exponential",
                          "suggested_backoff_seconds": wait,
                          "will_retry": not_found_streak <= max_404_retries,
                          "backoff_seconds": wait if not_found_streak <= max_404_retries else 0.0})
        if entry.get("http_status") != 200 or stop_reason == "request_error":
            append_journal(journal, entry)
            save_json(staging / "progress.json", {"updated_at": utc_now(), "last_confirmed_offset": max(confirmed_offsets, default=None),
                                                  "next_url": current, "next_offset": offset, "unique_ids": len(seen)})
            if entry.get("http_status") == 429 and entry["will_retry"]:
                time.sleep(entry["backoff_seconds"])
                continue
            if entry.get("review_resource_404") and entry["will_retry"]:
                time.sleep(entry["backoff_seconds"])
                continue
            stop_reason = "rate_limited_paused" if entry.get("http_status") == 429 else f"http_{entry.get('http_status')}" if entry.get("http_status") else "request_error"
            break
        try:
            payload = json.loads(body)
            check = inspect_page(payload, offset, seen, previous_oldest, app_id, storefront,
                                 cutoff, previous_ids)
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
        previous_ids = check["ids"]
        current = check["next_url"]
        retry_streak = 0
        not_found_streak = 0
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
               "review_404_attempts_this_run": review_404_attempts,
               "next_offset": offset_from_url(current) if current else None, "unique_ids": len(seen),
               "stop_reason": stop_reason, "cutoff": cutoff.isoformat() if cutoff else None}
    save_json(staging / "summary.json", summary)
    replay_run(staging, app_id, storefront, cutoff)
    return archive_web_staging(staging, archive_parent)


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
    frame = frame.drop_duplicates("id_review", keep="first").reset_index(drop=True)
    if len(frame) != len(state["ids"]) or frame.id_review.duplicated().any():
        raise ValueError("IDs Web duplicados ou divergentes do journal")
    frame["web_date_utc"] = pd.to_datetime(frame["web_date_utc"], utc=True, format="ISO8601")
    frame["web_requested_at"] = pd.to_datetime(frame["web_requested_at"], utc=True, format="ISO8601")
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
    action.add_argument("--recover-source-end", type=Path, metavar="CHECKPOINT")
    action.add_argument("--recover-continuity", type=Path, metavar="CHECKPOINT")
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--delay-seconds", type=float, default=15.0)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--max-429-retries", type=int, default=3)
    parser.add_argument("--max-404-retries", type=int, default=3)
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
    if args.recover_source_end:
        if not args.resume_from:
            parser.error("--recover-source-end requer --resume-from SOURCE_RUN")
        run = recover_source_end(args.bank, app_id, storefront, cutoff,
                                 args.resume_from, args.recover_source_end)
        print(run)
        print((run / "summary.json").read_text(encoding="utf-8"))
        return
    if args.recover_continuity:
        if not args.resume_from:
            parser.error("--recover-continuity requer --resume-from SOURCE_RUN")
        run = recover_continuity(args.bank, app_id, storefront, cutoff,
                                 args.resume_from, args.recover_continuity)
        print(run)
        print((run / "summary.json").read_text(encoding="utf-8"))
        return
    run = collect_web(args.bank, app_id, storefront, cutoff, args.delay_seconds,
                      args.max_pages, args.max_429_retries, args.resume_from,
                      args.max_404_retries)
    print(run)
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if summary["stop_reason"] in {"cutoff_reached", "source_end"}:
        print(json.dumps(materialize_web(args.bank, app_id, storefront, run), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
