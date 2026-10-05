"""Publicação App Store: preparação local, cópia conferida e recibo recuperável."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts.collect.storage import save_json, sha256
from scripts.collect.storage import assert_available, cleanup_staging


def validate(frame, allow_empty=False):
    if not {"id_review", "nota", "data_avaliacao"}.issubset(frame.columns):
        raise ValueError("Base App Store sem ID, nota ou data")
    if frame.empty and not allow_empty:
        raise ValueError("Base App Store vazia")
    ids = frame.id_review
    if ids.isna().any() or ids.astype(str).str.strip().eq("").any() or ids.duplicated().any():
        raise ValueError("IDs ausentes ou duplicados na base App Store")
    dates = pd.to_datetime(frame.data_avaliacao, utc=True, errors="coerce")
    if dates.isna().any() or not frame.nota.isin([1, 2, 3, 4, 5]).all():
        raise ValueError("Datas ou notas inválidas na base App Store")
    return dates


def publish(frame, output, snapshots, staging, state_file, expected_hash, identity, run_id, started_at):
    output, snapshots, staging, state_file = map(Path, (output, snapshots, staging, state_file))
    assert_available(output)
    assert_available(snapshots)
    assert_available(state_file)
    current_hash = sha256(output) if output.is_file() else None
    pending = staging / "publication_pending.json"
    # O checkpoint completo mantém o run_id após falha na gravação do estado.
    for receipt in (pending, state_file):
        if not receipt.is_file():
            continue
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if (saved.get("run_id") == run_id and saved.get("identity") == identity
                and saved.get("base_sha256") == current_hash):
            final = pd.read_parquet(output)
            validate(final)
            save_json(state_file, saved)
            cleanup_staging(staging, confirmed=True)
            pending.unlink(missing_ok=True)
            return final, saved
    if current_hash != expected_hash:
        raise RuntimeError("Base oficial mudou durante a coleta; publicação cancelada")
    validate(frame, allow_empty=True)
    frames = [frame]
    if current_hash:
        previous = pd.read_parquet(output)
        validate(previous)
        frames.append(previous)
    merged = pd.concat(frames, ignore_index=True).drop_duplicates("id_review", keep="first")
    merged["data_avaliacao"] = pd.to_datetime(merged.data_avaliacao, utc=True)
    merged = merged.sort_values("data_avaliacao", ascending=False).reset_index(drop=True)
    dates = validate(merged)
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / "reviews_merged.parquet"
    merged.to_parquet(staged, index=False)
    validate(pd.read_parquet(staged))
    final_hash = sha256(staged)
    snapshot = snapshots / f"reviews_raw_before_recollect_{datetime.now():%Y%m%d_%H%M%S_%f}.parquet" if current_hash else None
    state = {"schema_version": 1, "identity": identity, "run_id": run_id,
             "base_sha256": final_hash, "latest_review_at": dates.max().isoformat(),
             "last_successful_collection_at": datetime.now(timezone.utc).isoformat(),
             "collection_started_at": started_at, "reviews_in_base": len(merged),
             "snapshot": str(snapshot) if snapshot else None}
    save_json(pending, state)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="reviews_raw.", suffix=".tmp.parquet", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    snapshot_valid = False
    try:
        shutil.copy2(staged, temporary)
        if sha256(temporary) != final_hash:
            raise RuntimeError("SHA-256 divergente na cópia para o destino oficial")
        if (sha256(output) if output.is_file() else None) != expected_hash:
            raise RuntimeError("Base oficial mudou antes da substituição; publicação cancelada")
        if snapshot:
            snapshots.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output, snapshot)
            if sha256(snapshot) != current_hash:
                raise RuntimeError("SHA-256 divergente no snapshot; publicação cancelada")
            snapshot_valid = True
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
        if snapshot and not snapshot_valid:
            snapshot.unlink(missing_ok=True)
    save_json(state_file, state)
    cleanup_staging(staging, confirmed=True)
    pending.unlink()
    return merged, state
