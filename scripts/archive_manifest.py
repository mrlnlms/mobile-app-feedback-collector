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


def describe(path, kind, platform, bank=None):
    frame = pd.read_parquet(path)
    if "id_review" not in frame or "data_avaliacao" not in frame:
        raise ValueError(f"Colunas essenciais ausentes: {path}")
    if frame["id_review"].duplicated().any():
        raise ValueError(f"IDs duplicados: {path}")
    dates = pd.to_datetime(frame["data_avaliacao"], errors="coerce", utc=True)
    if dates.isna().any():
        raise ValueError(f"Datas inválidas: {path}")
    return {
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
