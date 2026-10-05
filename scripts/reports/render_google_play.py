"""Renderiza o Quarto localmente e preserva uma versão completa no Drive.

Uso: venv/bin/python -m scripts.reports.render_google_play
"""

import argparse
import fcntl
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from scripts.collect.storage import assert_available, assert_local, sha256


def replace_link(link, target):
    if link.exists() and not link.is_symlink():
        raise RuntimeError(f"Caminho existente não é symlink: {link}")
    temporary = link.with_name(link.name + ".tmp-link")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


REPORTS = {
    'google_play': ('google-play-descritiva', 'output', 'google_play'),
    'app_store': ('app-store-descritiva', 'app-store-output', 'app_store'),
}


def render(root, platform='google_play'):
    root = Path(root).resolve()
    stem, link_name, data_platform = REPORTS[platform]
    source = root / 'analysis' / f'{stem}.qmd'
    destination = root / 'private/reports' / stem
    assert_available(destination)
    if not (root / "private").is_dir():
        raise FileNotFoundError("Configure private/ com o acervo externo antes de renderizar")
    workspace = root / ".runtime/reports"
    assert_local((workspace,), destination)
    workspace.mkdir(parents=True, exist_ok=True)
    link = root / "analysis" / link_name
    # Checa o ponto de acesso antes de produzir uma versão.
    if link.exists() and not link.is_symlink():
        raise RuntimeError(f"Migre a saída local para o Drive antes de renderizar: {link}")
    with (workspace / "render.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Já existe uma renderização local em andamento") from exc
        with tempfile.TemporaryDirectory(prefix="render-", dir=workspace) as directory:
            staged = Path(directory)
            staged_source = staged / source.name
            shutil.copy2(source, staged_source)
            env = os.environ.copy()
            env['QUARTO_PYTHON'] = str(root / 'venv/bin/python')
            subprocess.run(['quarto', 'render', str(staged_source), '--execute-dir', str(root),
                            '--execute-daemon', '0'],
                           cwd=root, env=env, check=True)
            html = staged / f'{stem}.html'
            if not html.is_file() or not (staged / f'{stem}_files').is_dir():
                raise RuntimeError('Quarto não gerou o HTML e seus recursos esperados')
            comparison = root / 'data/derived' / data_platform / 'current/collection_comparison.json'
            if comparison.is_file():
                shutil.copy2(comparison, staged / comparison.name)
            version = datetime.now().strftime('%Y-%m-%dT%H%M%S_%f')
            published = destination / version
            destination.mkdir(parents=True, exist_ok=True)
            pending = destination / ('.' + version + '.pending')
            try:
                shutil.copytree(staged, pending)
                for file in staged.rglob('*'):
                    if file.is_file() and sha256(file) != sha256(pending / file.relative_to(staged)):
                        raise RuntimeError(f'Hash divergente no relatório: {file.name}')
                os.replace(pending, published)
            finally:
                if pending.exists():
                    shutil.rmtree(pending)
            replace_link(link, os.path.relpath(published, link.parent))
            print(f'Relatório preservado: {published}')
            print(f'Abrir: {link / html.name}')
            return published


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    render(Path(__file__).resolve().parents[2])


if __name__ == '__main__':
    main()
