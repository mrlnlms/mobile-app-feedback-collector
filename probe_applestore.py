"""Repete um banco da App Store sem modificar a base principal.

Uso: venv/bin/python probe_applestore.py --bank caixa
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from collector_applestore import collect_bank, save_json_atomic


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def latest_bank_report(output_dir, app_id):
    reports = []
    for path in output_dir.glob("report_*.json"):
        for item in json.loads(path.read_text(encoding="utf-8")):
            if str(item.get("apple_app_id")) == str(app_id) and item.get("status") == "concluído":
                reports.append((item.get("fim_execucao", ""), path, item))
    return max(reports, default=None, key=lambda x: x[0])


def sort_counts(frame, sorts):
    if frame.empty:
        return {sort: 0 for sort in sorts}
    found = frame["sorts_encontrados"].fillna("")
    return {sort: int(found.str.split(",").map(lambda parts: sort in parts).sum()) for sort in sorts}


def date_span(frame):
    if frame.empty:
        return {"mais_antiga": None, "mais_recente": None}
    dates = pd.to_datetime(frame["data_avaliacao"], utc=True)
    return {"mais_antiga": dates.min().isoformat(), "mais_recente": dates.max().isoformat()}


def compare(original, repeated, sorts):
    before = pd.read_parquet(original)
    after = pd.read_parquet(repeated)
    if before["id_review"].duplicated().any() or after["id_review"].duplicated().any():
        raise ValueError("ID duplicado em uma das bases")
    old_ids = set(before["id_review"].astype(str))
    new_ids = set(after["id_review"].astype(str))
    old_by_id = before.set_index("id_review")
    new_by_id = after.set_index("id_review")
    fields = ("nota", "data_avaliacao", "titulo", "texto_avaliacao")
    changed = [review_id for review_id in sorted(old_ids & new_ids)
               if any(str(old_by_id.at[review_id, field]) != str(new_by_id.at[review_id, field])
                      for field in fields)]
    return {
        "base_original": {"total": len(before), "datas_utc": date_span(before), "reviews_por_sort": sort_counts(before, sorts)},
        "nova_coleta": {"total": len(after), "datas_utc": date_span(after), "reviews_por_sort": sort_counts(after, sorts)},
        "ids_em_ambas": len(old_ids & new_ids),
        "ids_apenas_na_original": sorted(old_ids - new_ids),
        "ids_apenas_na_nova": sorted(new_ids - old_ids),
        "ids_em_ambas_com_campos_alterados": changed,
    }


def run_probe(bank_key, config, probe_parent):
    banks = config["banks"]
    if bank_key not in banks:
        raise ValueError(f"Banco desconhecido: {bank_key}")
    settings = config["app_store"].copy()
    canonical_root = Path(settings["output_dir"]).resolve()
    original = canonical_root / bank_key / "reviews_raw.parquet"
    if not original.is_file():
        raise FileNotFoundError(f"Base original ausente: {original}")
    original_hash = sha256(original)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_root = probe_parent.resolve() / f"{bank_key}_{run_id}"
    run_root.mkdir(parents=True, exist_ok=False)
    settings["output_dir"] = str(run_root)
    settings["checkpoint_dir"] = str(run_root / "checkpoints")
    repeated = run_root / bank_key / "reviews_raw.parquet"
    manifest_path = run_root / "probe.json"
    manifest = {
        "bank": bank_key,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "em andamento",
        "original_file": str(original),
        "original_sha256_before": original_hash,
        "new_file": str(repeated),
        "settings": {key: settings[key] for key in ("country", "sorts", "max_pages_per_sort", "delay_seconds", "max_retries")},
    }
    save_json_atomic(manifest_path, manifest)
    try:
        report = collect_bank(bank_key, banks[bank_key], settings, allow_empty=True)
        comparison = compare(original, repeated, settings["sorts"])
        previous = latest_bank_report(canonical_root, banks[bank_key]["apple_app_id"])
        comparison["paginas_por_sort_original"] = previous[2].get("paginas_por_sort") if previous else None
        comparison["paginas_por_sort_nova"] = report["paginas_por_sort"]
        comparison["relatorio_original"] = str(previous[1]) if previous else None
        original_hash_after = sha256(original)
        if original_hash_after != original_hash:
            raise RuntimeError("A base original mudou durante o teste; comparação requer revisão")
        manifest.update({
            "status": "concluído", "finished_at": datetime.now(timezone.utc).isoformat(),
            "original_sha256_after": original_hash_after,
            "original_preserved": True, "collection_report": report,
            "comparison": comparison,
        })
        save_json_atomic(manifest_path, manifest)
        print(f"\nTeste concluído: {run_root}")
        print(f"Base original preservada (SHA-256 igual): {original}")
        print(f"Páginas por sort: {comparison['paginas_por_sort_original']} → {comparison['paginas_por_sort_nova']}")
        print(f"Reviews: {comparison['base_original']['total']} → {comparison['nova_coleta']['total']}")
        print(f"IDs em ambas: {comparison['ids_em_ambas']}; só na original: {len(comparison['ids_apenas_na_original'])}; só na nova: {len(comparison['ids_apenas_na_nova'])}")
        print(f"Comparação completa: {manifest_path}")
        return run_root
    except BaseException as exc:
        manifest.update({"status": "interrompido", "finished_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)})
        if original.is_file():
            manifest["original_sha256_after"] = sha256(original)
            manifest["original_preserved"] = manifest["original_sha256_after"] == original_hash
        save_json_atomic(manifest_path, manifest)
        print(f"Teste interrompido; arquivos parciais em {run_root}", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", required=True, help="Um único banco definido em config.yaml")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    probe_parent = Path(config["app_store"]["output_dir"]) / "experiments"
    run_probe(args.bank, config, probe_parent)


if __name__ == "__main__":
    main()
