"""Coleta reviews do RSS público da App Store para os bancos em config.yaml.

Uso: venv/bin/python -m scripts.collect.app_store --bank nubank
     venv/bin/python -m scripts.collect.app_store --all
"""

import argparse
import fcntl
import json
import logging
import os
import shutil
import tempfile
import uuid
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
import yaml

from scripts.collect import app_store_storage as storage
from scripts.collect.storage import sha256
from scripts.collect.storage import assert_available, assert_local


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("app_store")
SUPPORTED_SORTS = {"mostrecent", "mosthelpful", "mostcritical", "mostfavourable", "mostfavorable"}
COLS = ["id_review", "usuario", "nota", "data_avaliacao", "titulo", "texto_avaliacao", "versao_app", "sorts_encontrados", "apple_app_id", "pais", "plataforma"]


def fetch_page(app_id, country, sort, page, max_retries):
    url = f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby={sort}/json"
    for attempt in range(1, max_retries + 1):
        try:
            request = Request(url, headers={"User-Agent": "playstore-feedbacks/1.0"})
            with urlopen(request, timeout=20) as response:
                data = json.load(response)
            feed = data.get("feed")
            if not isinstance(feed, dict):
                raise ValueError("Resposta sem objeto feed")
            entries = feed.get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                raise ValueError("Formato inesperado de feed.entry")
            return [entry for entry in entries if "im:rating" in entry]
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if attempt == max_retries:
                raise RuntimeError(f"Falha em {sort}, página {page}, após {attempt} tentativas: {exc}") from exc
            delay = min(2 ** attempt, 30)
            log.warning("%s página %s: %s; nova tentativa em %ss", sort, page, exc, delay)
            time.sleep(delay)


def label(entry, key):
    return entry.get(key, {}).get("label", "")


def parse_review(entry, app_id, country, sort):
    review_id = label(entry, "id")
    updated = label(entry, "updated")
    if not review_id or not updated:
        raise ValueError(f"Review sem ID ou updated no sort {sort}")
    stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError(f"Review {review_id} sem timezone")
    return {
        "id_review": review_id,
        "usuario": label(entry.get("author", {}), "name"),
        "nota": int(label(entry, "im:rating")),
        "data_avaliacao": updated,
        "titulo": label(entry, "title"),
        "texto_avaliacao": label(entry, "content"),
        "versao_app": label(entry, "im:version"),
        "sorts_encontrados": sort,
        "apple_app_id": app_id,
        "pais": country,
        "plataforma": "iOS",
    }


def save_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def publish(records, output_file, start_date, snapshot_dir=None, *, staging_dir=None,
            state_file=None, expected_hash=None, identity=None, run_id=None, started_at=None):
    output_file = Path(output_file)
    assert_available(output_file)
    frame = pd.DataFrame(records, columns=COLS)
    frame["data_avaliacao"] = pd.to_datetime(frame["data_avaliacao"], utc=True)
    # Filtra pela data civil do RSS antes da conversão para UTC.
    keep = [datetime.fromisoformat(r["data_avaliacao"].replace("Z", "+00:00")).date() >= start_date
            for r in records]
    frame = frame.loc[keep]
    snapshots = Path(snapshot_dir) if snapshot_dir is not None else output_file.parent / "snapshots"
    if staging_dir is None:
        # Compatibilidade para chamadas isoladas: a preparação nunca fica no destino.
        with tempfile.TemporaryDirectory(prefix="app-store-publication-") as directory:
            final, _ = storage.publish(frame, output_file, snapshots, Path(directory),
                                      Path(directory) / "state.json",
                                      sha256(output_file) if output_file.is_file() else None,
                                      {}, str(uuid.uuid4()), datetime.now(timezone.utc).isoformat())
            return final
    return storage.publish(frame, output_file, snapshots, staging_dir, state_file,
                           expected_hash, identity, run_id, started_at)


def collect_bank(key, bank, settings, allow_empty=False):
    output = Path(settings["output_dir"]) / key / "reviews_raw.parquet"
    assert_available(output)
    staging = Path(settings.get("staging_dir", ".runtime/app_store/staging")) / key
    assert_local((staging, settings["checkpoint_dir"]), settings["output_dir"])
    staging.mkdir(parents=True, exist_ok=True)
    with (staging / "collection.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Já existe uma coleta local em andamento para {key}") from exc
        return _collect_bank(key, bank, settings, allow_empty)


def _collect_bank(key, bank, settings, allow_empty=False):
    app_id = str(bank["apple_app_id"])
    if not app_id.isdecimal():
        raise ValueError(f"apple_app_id inválido para {key}: {app_id}")
    start_date = date.fromisoformat(bank["start_date"])
    country = settings["country"].lower()
    sorts = settings["sorts"]
    if not sorts or any(sort not in SUPPORTED_SORTS for sort in sorts):
        raise ValueError(f"Sorts inválidos: {sorts}")
    max_pages = int(settings["max_pages_per_sort"])
    if not 1 <= max_pages <= 10:
        raise ValueError("max_pages_per_sort deve estar entre 1 e 10")
    checkpoint = Path(settings["checkpoint_dir"]) / f"{key}_checkpoint.json"
    identity = {"app_id": app_id, "country": country, "sorts": sorts, "max_pages": max_pages, "start_date": str(start_date)}
    output_file = Path(settings["output_dir"]) / key / "reviews_raw.parquet"
    state = {"identity": identity, "sort_index": 0, "page": 1, "reviews": [],
             "pages": {sort: 0 for sort in sorts}, "run_id": str(uuid.uuid4()),
             "started_at": datetime.now(timezone.utc).isoformat(),
             "base_sha256": sha256(output_file) if output_file.is_file() else None}
    if checkpoint.exists():
        loaded = json.loads(checkpoint.read_text(encoding="utf-8"))
        if loaded.get("identity") != identity:
            raise ValueError(f"Checkpoint incompatível: {checkpoint}")
        state = loaded
        if not {"run_id", "started_at", "base_sha256"}.issubset(state):
            raise ValueError(f"Checkpoint legado requer revisão antes da publicação: {checkpoint}")
        if state["sort_index"] == len(sorts) and not state["reviews"]:
            # Sem dados a retomar: o mesmo comando deve poder consultar os feeds de novo.
            state = {"identity": identity, "sort_index": 0, "page": 1, "reviews": [],
                     "pages": {sort: 0 for sort in sorts}, "run_id": str(uuid.uuid4()),
                     "started_at": datetime.now(timezone.utc).isoformat(),
                     "base_sha256": sha256(output_file) if output_file.is_file() else None}
        if state["sort_index"] < len(sorts):
            log.info("Retomando %s em %s página %s", key, sorts[state["sort_index"]], state["page"])
        else:
            log.info("Retomando publicação de %s", key)
    log.info("Coletando %s (%s), desde %s", bank.get("name", key), app_id, start_date)
    started = datetime.fromisoformat(state["started_at"])
    save_json_atomic(checkpoint, state)
    for index in range(state["sort_index"], len(sorts)):
        sort = sorts[index]
        first_page = state["page"] if index == state["sort_index"] else 1
        for page in range(first_page, max_pages + 1):
            entries = fetch_page(app_id, country, sort, page, int(settings["max_retries"]))
            if not entries:
                log.info("%s página %s vazia; fim deste sort", sort, page)
                break
            state["reviews"].extend(parse_review(entry, app_id, country, sort) for entry in entries)
            state["pages"][sort] += 1
            state["sort_index"] = index
            state["page"] = page + 1
            save_json_atomic(checkpoint, state)
            log.info("%s página %s: %s reviews; bruto %s", sort, page, len(entries), len(state["reviews"]))
            if page < max_pages:
                time.sleep(float(settings["delay_seconds"]))
        state["sort_index"] = index + 1
        state["page"] = 1
        save_json_atomic(checkpoint, state)
        if index + 1 < len(sorts):
            time.sleep(float(settings["delay_seconds"]))
    if not state["reviews"]:
        if not allow_empty:
            raise RuntimeError(f"Nenhuma review retornada para {key}; checkpoint preservado")
        output_file = Path(settings["output_dir"]) / key / "reviews_raw.parquet"
        if output_file.exists():
            raise RuntimeError("Experimento vazio não pode substituir uma base existente")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        empty = pd.DataFrame(columns=COLS)
        empty["data_avaliacao"] = pd.to_datetime(empty["data_avaliacao"], utc=True)
        staging = Path(settings.get("staging_dir", ".runtime/app_store/staging")) / key
        with tempfile.TemporaryDirectory(prefix="empty-probe-", dir=staging) as directory:
            staged = Path(directory) / "reviews_raw.parquet"
            empty.to_parquet(staged, index=False)
            temporary = output_file.with_name("reviews_raw.tmp.parquet")
            try:
                shutil.copy2(staged, temporary)
                if sha256(staged) != sha256(temporary):
                    raise RuntimeError("SHA-256 divergente no experimento vazio")
                os.replace(temporary, output_file)
            finally:
                temporary.unlink(missing_ok=True)
        checkpoint.unlink()
        return {
            "banco": bank.get("name", key), "apple_app_id": app_id, "pais": country,
            "data_inicio_filtro": str(start_date), "inicio_execucao": started.isoformat(),
            "fim_execucao": datetime.now(timezone.utc).isoformat(), "status": "sem reviews",
            "sorts": sorts, "paginas_por_sort": state["pages"],
            "sorts_sem_reviews": sorts, "reviews_brutos": 0, "reviews_unicos": 0,
            "reviews_no_periodo_nesta_coleta": 0, "reviews_na_base": 0,
            "data_mais_antiga_observada": None, "data_mais_recente_observada": None,
            "mostrecent_data_mais_antiga": None, "mostrecent_alcancou_inicio": None,
            "limite_do_feed": "Até 10 páginas por sort; não equivale ao histórico completo",
        }
    unique = {}
    for review in state["reviews"]:
        existing = unique.get(review["id_review"])
        if existing:
            found = set(existing["sorts_encontrados"].split(","))
            found.add(review["sorts_encontrados"])
            existing["sorts_encontrados"] = ",".join(sort for sort in sorts if sort in found)
        else:
            unique[review["id_review"]] = review
    dated = [r for r in unique.values() if datetime.fromisoformat(r["data_avaliacao"].replace("Z", "+00:00")).date() >= start_date]
    output_file = Path(settings["output_dir"]) / key / "reviews_raw.parquet"
    staging = Path(settings.get("staging_dir", ".runtime/app_store/staging")) / key
    state_file = Path(settings.get("state_dir", "data/runs/app_store/state")) / f"{key}.json"
    final, published = publish(
        dated, output_file, start_date, Path(settings["snapshot_dir"]) / key,
        staging_dir=staging, state_file=state_file, expected_hash=state["base_sha256"],
        identity=identity, run_id=state["run_id"], started_at=state["started_at"],
    )
    checkpoint.unlink()
    dates = [datetime.fromisoformat(r["data_avaliacao"].replace("Z", "+00:00")).date() for r in unique.values()]
    recent = [datetime.fromisoformat(r["data_avaliacao"].replace("Z", "+00:00")).date()
              for r in state["reviews"] if r["sorts_encontrados"] == "mostrecent"]
    report = {
        "banco": bank.get("name", key), "apple_app_id": app_id, "pais": country,
        "data_inicio_filtro": str(start_date), "inicio_execucao": started.isoformat(),
        "fim_execucao": datetime.now(timezone.utc).isoformat(), "status": "concluído",
        "sorts": sorts, "paginas_por_sort": state["pages"],
        "sorts_sem_reviews": [sort for sort in sorts if state["pages"][sort] == 0],
        "reviews_brutos": len(state["reviews"]), "reviews_unicos": len(unique),
        "reviews_no_periodo_nesta_coleta": len(dated), "reviews_na_base": len(final),
        "base_sha256": published["base_sha256"], "estado": str(state_file),
        "snapshot": published["snapshot"],
        "publicacao": "gravada e conferida no destino; sincronização da nuvem não verificada",
        "data_mais_antiga_observada": str(min(dates)),
        "data_mais_recente_observada": str(max(dates)),
        "mostrecent_data_mais_antiga": str(min(recent)) if recent else None,
        "mostrecent_alcancou_inicio": min(recent) <= start_date if recent else None,
        "limite_do_feed": "Até 10 páginas por sort; não equivale ao histórico completo",
    }
    log.info("%s: %s brutos, %s únicos, %s no período; base %s", key, len(state["reviews"]), len(unique), len(dated), len(final))
    return report


def main():
    parser = argparse.ArgumentParser(description="Coleta reviews da Apple App Store")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bank", nargs="+", help="Bancos definidos em config.yaml")
    group.add_argument("--all", action="store_true", help="Todos os bancos")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    banks, settings = config["banks"], config["app_store"]
    keys = list(banks) if args.all else args.bank
    unknown = [key for key in keys if key not in banks]
    if unknown:
        parser.error(f"Bancos desconhecidos: {', '.join(unknown)}")
    reports = []
    failed = False
    for key in keys:
        try:
            reports.append(collect_bank(key, banks[key], settings))
        except KeyboardInterrupt:
            log.warning("Interrompido; checkpoint preservado")
            return 130
        except Exception as exc:
            failed = True
            log.exception("Falha na coleta de %s", key)
            reports.append({"banco": banks[key].get("name", key), "apple_app_id": banks[key].get("apple_app_id"), "status": "interrompido por erro", "erro": str(exc)})
    report_path = Path(settings["report_dir"]) / f"report_{datetime.now():%Y%m%d_%H%M%S}.json"
    save_json_atomic(report_path, reports)
    log.info("Relatório: %s", report_path)
    if any(report["status"] == "concluído" for report in reports):
        from scripts.archive_manifest import build
        try:
            save_json_atomic(Path(settings.get("manifest_file", "data/manifest.json")), build(config))
        except Exception as exc:
            log.error("Bases publicadas, mas atualização do manifesto falhou: %s", exc)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
