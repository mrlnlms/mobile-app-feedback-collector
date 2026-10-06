"""Registra hashes e cobertura observada dos Parquets locais sem copiar reviews para o Git."""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(path, kind, platform, bank=None, date_column="data_avaliacao"):
    frame = pd.read_parquet(path)
    if "id_review" not in frame or date_column not in frame:
        raise ValueError(f"Colunas essenciais ausentes: {path}")
    if frame["id_review"].duplicated().any():
        raise ValueError(f"IDs duplicados: {path}")
    dates = pd.to_datetime(frame[date_column], errors="coerce", utc=True)
    if dates.isna().any():
        raise ValueError(f"Datas inválidas: {path}")
    item = {
        "path": path.as_posix(),
        "kind": kind,
        "platform": platform,
        "bank": bank,
        "size_bytes": path.stat().st_size,
        "sha256": sha256(path),
        "reviews": len(frame),
        "oldest_utc": dates.min().isoformat() if len(dates) else None,
        "newest_utc": dates.max().isoformat() if len(dates) else None,
    }
    if date_column != "data_avaliacao":
        item["date_field"] = date_column
    return item


def describe_reconciled(path, bank, kind="reconciled"):
    frame = pd.read_parquet(path)
    required = {"id_review", "source_presence", "rss_updated_utc", "web_date_utc"}
    if not required.issubset(frame.columns) or frame.id_review.isna().any() or frame.id_review.duplicated().any():
        raise ValueError(f"Corpus reconciliado inválido: {path}")
    counts = frame.source_presence.value_counts().to_dict()
    if set(counts) - {"rss_web", "web_only", "rss_only"}:
        raise ValueError(f"Proveniência inválida: {path}")
    item = {"path": path.as_posix(), "kind": kind, "platform": "app_store",
            "bank": bank, "size_bytes": path.stat().st_size, "sha256": sha256(path),
            "reviews": len(frame), "source_counts": counts}
    for column in ("rss_updated_utc", "web_date_utc"):
        dates = pd.to_datetime(frame[column], errors="coerce", utc=True).dropna()
        item[column + "_min"] = dates.min().isoformat() if not dates.empty else None
        item[column + "_max"] = dates.max().isoformat() if not dates.empty else None
    return item


def build(config):
    files = []
    for platform, key in (("google_play", "collection"), ("app_store", "app_store")):
        root = Path(config[key]["output_dir"])
        for bank in config["banks"]:
            path = root / bank / "reviews_raw.parquet"
            if not path.is_file():
                raise FileNotFoundError(path)
            files.append(describe(path, "primary", platform, bank))
        snapshots = Path(config[key]["snapshot_dir"])
        for path in sorted(snapshots.glob("*/*.parquet")):
            files.append(describe(path, "snapshot", platform, path.parent.name))
    experiments = Path(config["app_store"]["probe_dir"])
    for path in sorted(experiments.glob("*/*/reviews_raw.parquet")):
        files.append(describe(path, "probe", "app_store", path.parent.name))
    app_store_root = Path(config["app_store"]["output_dir"])
    for bank in config["banks"]:
        web_path = app_store_root / bank / "reviews_web.parquet"
        if web_path.is_file():
            files.append(describe(web_path, "web", "app_store", bank, "web_date_utc"))
        reconciled_path = Path("data/derived/app_store") / bank / "reviews_reconciled.parquet"
        if reconciled_path.is_file():
            files.append(describe_reconciled(reconciled_path, bank))
    for path in sorted(Path("data/runs/app_store/snapshots_web").glob("*/*.parquet")):
        files.append(describe(path, "snapshot_web", "app_store", path.parent.name, "web_date_utc"))
    for path in sorted(Path("data/runs/app_store/snapshots_reconciled").glob("*/*.parquet")):
        files.append(describe_reconciled(path, path.parent.name, "snapshot_reconciled"))
    return {"generated_at": datetime.now().astimezone().isoformat(), "files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="data/manifest.json")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    manifest = build(config)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(manifest['files'])} Parquets registrados em {destination}")


if __name__ == "__main__":
    main()
