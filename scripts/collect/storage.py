"""Guardas do destino oficial e dos arquivos de trabalho locais."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def assert_available(path):
    path = Path(path)
    for entry in (path, *path.parents):
        if entry.is_symlink() and not entry.exists():
            raise FileNotFoundError(f"Destino oficial indisponível: {entry}")


def assert_local(paths, official):
    official = Path(official).resolve()
    for path in paths:
        resolved = Path(path).resolve()
        if resolved.is_relative_to(official) or "CloudStorage" in resolved.parts:
            raise ValueError("Staging e checkpoints precisam ficar fora do Drive e da base oficial")


def cleanup_staging(directory, confirmed=False):
    """Remove só o Parquet preparado; recibos pendentes impedem a limpeza."""
    directory = Path(directory)
    if confirmed or not (directory / "publication_pending.json").exists():
        (directory / "reviews_merged.parquet").unlink(missing_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value):
    """Substitui um JSON apenas depois de escrever o documento completo."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
