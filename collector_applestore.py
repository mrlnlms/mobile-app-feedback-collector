"""Coleta reviews do RSS público da App Store para os bancos em config.yaml.

Uso: venv/bin/python collector_applestore.py --bank nubank
     venv/bin/python collector_applestore.py --all
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd
import yaml


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


def publish(records, output_file, start_date):
    frame = pd.DataFrame(records, columns=COLS)
    frame["data_avaliacao"] = pd.to_datetime(frame["data_avaliacao"], utc=True)
    # A data do filtro é a data civil informada pelo feed, antes da conversão para UTC.
    keep = [datetime.fromisoformat(record["data_avaliacao"].replace("Z", "+00:00")).date() >= start_date
            for record in records]
    frame = frame.loc[keep]
    if output_file.exists():
        previous = pd.read_parquet(output_file)
        frame = pd.concat([frame, previous], ignore_index=True)
    frame = frame.drop_duplicates(subset="id_review", keep="first")
    frame = frame.sort_values("data_avaliacao", ascending=False).reset_index(drop=True)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    temp = output_file.with_name("reviews_raw.tmp.parquet")
    frame.to_parquet(temp, index=False)
    if output_file.exists():
        snapshots = output_file.parent / "snapshots"
        snapshots.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(output_file, snapshots / f"reviews_raw_before_recollect_{stamp}.parquet")
    os.replace(temp, output_file)
    return frame


def collect_bank(key, bank, settings, allow_empty=False):
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
    state = {"identity": identity, "sort_index": 0, "page": 1, "reviews": [], "pages": {sort: 0 for sort in sorts}}
    if checkpoint.exists():
        loaded = json.loads(checkpoint.read_text(encoding="utf-8"))
        if loaded.get("identity") != identity:
            raise ValueError(f"Checkpoint incompatível: {checkpoint}")
        state = loaded
        if state["sort_index"] < len(sorts):
            log.info("Retomando %s em %s página %s", key, sorts[state["sort_index"]], state["page"])
        else:
            log.info("Retomando publicação de %s", key)
    log.info("Coletando %s (%s), desde %s", bank.get("name", key), app_id, start_date)
    started = datetime.now(timezone.utc)
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
        output_file.parent.mkdir(parents=True, exist_ok=True)
        empty = pd.DataFrame(columns=COLS)
        empty["data_avaliacao"] = pd.to_datetime(empty["data_avaliacao"], utc=True)
        empty.to_parquet(output_file, index=False)
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
    final = publish(dated, output_file, start_date)
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
    report_path = Path(settings["output_dir"]) / f"report_{datetime.now():%Y%m%d_%H%M%S}.json"
    save_json_atomic(report_path, reports)
    log.info("Relatório: %s", report_path)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
