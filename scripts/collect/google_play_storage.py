"""Referência incremental e publicação de bases Google Play preparadas localmente."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from scripts.collect.storage import cleanup_staging, save_json, sha256


def validate(frame):
    required = {"id_review", "data_avaliacao", "nota"}
    if not required.issubset(frame.columns):
        raise ValueError("Base Google Play sem ID, data ou nota")
    if frame.empty:
        raise ValueError("Base Google Play vazia")
    ids = frame["id_review"]
    if ids.isna().any() or ids.astype(str).str.strip().eq("").any() or ids.duplicated().any():
        raise ValueError("IDs ausentes ou duplicados na base Google Play")
    dates = pd.to_datetime(frame["data_avaliacao"], errors="coerce")
    if dates.isna().any() or dates.dt.tz is not None:
        raise ValueError("Datas inválidas ou com timezone inesperado na base Google Play")
    if not frame["nota"].isin([1, 2, 3, 4, 5]).all():
        raise ValueError("Notas inválidas na base Google Play")
    return dates


def collection_reference(output_file, state_file, app_id, historical_start, overlap_hours=24):
    """Usa estado correspondente ao hash, ou reconstrói a referência pelo Parquet."""
    output_file, state_file = Path(output_file), Path(state_file)
    for parent in output_file.parents:
        if parent.is_symlink() and not parent.exists():
            raise FileNotFoundError(f"Destino oficial indisponível: {parent}")
    if overlap_hours != 24:
        raise ValueError("A sobreposição aprovada para Google Play é de 24 horas")
    base_hash = sha256(output_file) if output_file.is_file() else None
    latest = None
    origin = "primeira coleta"
    if base_hash:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if (state["schema_version"] == 1 and state["app_id"] == app_id
                    and state["base_sha256"] == base_hash):
                latest = pd.Timestamp(state["latest_review_at"])
                if pd.isna(latest) or latest.tzinfo is not None:
                    raise ValueError("Data inválida no estado")
                origin = "estado"
        except (OSError, ValueError, KeyError, TypeError):
            latest = None
        if latest is None:
            frame = pd.read_parquet(output_file, columns=["id_review", "nota", "data_avaliacao"])
            latest = validate(frame).max()
            origin = "base"
    cutoff = max(historical_start, latest.to_pydatetime() - timedelta(hours=24)) if latest is not None else historical_start
    return {
        "base_sha256": base_hash,
        "latest_review_at": latest.isoformat() if latest is not None else None,
        "cutoff": cutoff,
        "origin": origin,
    }


def recover_publication(output_file, state_file, staging_dir, app_id):
    """Recupera estado se a base foi publicada antes de uma interrupção no JSON."""
    pending = Path(staging_dir) / "publication_pending.json"
    if not pending.is_file() or not Path(output_file).is_file():
        return False
    state = json.loads(pending.read_text(encoding="utf-8"))
    if state["app_id"] != app_id or state["base_sha256"] != sha256(output_file):
        return False
    save_json(state_file, state)
    cleanup_staging(staging_dir, confirmed=True)
    pending.unlink()
    return True


def publish(new_reviews, output_file, snapshot_dir, staging_dir, state_file,
            app_id, historical_start, reference, started_at):
    """Mescla localmente; confere a cópia antes de substituir a base oficial."""
    output_file = Path(output_file)
    snapshot_dir, staging_dir, state_file = map(Path, (snapshot_dir, staging_dir, state_file))
    dates = validate(new_reviews)
    fresh = new_reviews.loc[dates >= historical_start].copy()
    frames = [fresh]
    expected_hash = reference["base_sha256"]
    current_hash = sha256(output_file) if output_file.is_file() else None
    if current_hash != expected_hash:
        raise RuntimeError("Base oficial mudou durante a coleta; publicação cancelada")
    if current_hash:
        previous = pd.read_parquet(output_file)
        validate(previous)
        frames.append(previous)
    merged = pd.concat(frames, ignore_index=True).drop_duplicates("id_review", keep="first")
    merged["data_avaliacao"] = pd.to_datetime(merged["data_avaliacao"])
    merged = merged.sort_values("data_avaliacao", ascending=False).reset_index(drop=True)
    final_dates = validate(merged)
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / "reviews_merged.parquet"
    merged.to_parquet(staged, index=False)
    validate(pd.read_parquet(staged))
    staged_hash = sha256(staged)
    state = {
        "schema_version": 1,
        "app_id": app_id,
        "base_sha256": staged_hash,
        "latest_review_at": final_dates.max().isoformat(),
        "last_successful_collection_at": datetime.now().astimezone().isoformat(),
        "collection_started_at": started_at,
        "collection_cutoff": reference["cutoff"].isoformat(),
        "overlap_hours": 24,
        "reviews_in_base": len(merged),
    }
    pending = staging_dir / "publication_pending.json"
    save_json(pending, state)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="reviews_raw.", suffix=".tmp.parquet", dir=output_file.parent)
    os.close(descriptor)
    destination_temp = Path(name)
    snapshot = None
    snapshot_valid = False
    try:
        shutil.copy2(staged, destination_temp)
        if sha256(destination_temp) != staged_hash:
            raise RuntimeError("SHA-256 divergente na cópia para o destino oficial")
        current_hash = sha256(output_file) if output_file.is_file() else None
        if current_hash != expected_hash:
            raise RuntimeError("Base oficial mudou antes da substituição; publicação cancelada")
        if current_hash:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            snapshot = snapshot_dir / f"reviews_raw_before_recollect_{datetime.now():%Y%m%d_%H%M%S_%f}.parquet"
            shutil.copy2(output_file, snapshot)
            if sha256(snapshot) != current_hash:
                raise RuntimeError("SHA-256 divergente no snapshot; publicação cancelada")
            snapshot_valid = True
        os.replace(destination_temp, output_file)
    finally:
        destination_temp.unlink(missing_ok=True)
        if snapshot is not None and not snapshot_valid:
            snapshot.unlink(missing_ok=True)
    # Uma interrupção aqui é recuperada pelo recibo local da publicação.
    save_json(state_file, state)
    cleanup_staging(staging_dir, confirmed=True)
    pending.unlink()
    return merged, state, snapshot
