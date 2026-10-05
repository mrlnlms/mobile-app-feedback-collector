"""Configura os caminhos locais do acervo externo sem sobrescrever arquivos divergentes."""

import argparse
import os
import shutil
from datetime import datetime
from pathlib import Path

from scripts.collect.storage import assert_available, save_json, sha256
from scripts.reports.render_google_play import replace_link


def files(root):
    for directory, _, names in os.walk(root, followlinks=True):
        for name in names:
            path = Path(directory) / name
            if path.is_file():
                yield path


def copy_verified(source, destination):
    if destination.exists():
        if sha256(source) != sha256(destination):
            raise RuntimeError(f'Arquivos divergentes; migração cancelada: {source} / {destination}')
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.migration-tmp')
    try:
        shutil.copy2(source, temporary)
        if sha256(source) != sha256(temporary):
            raise RuntimeError(f'Hash divergente na migração: {source}')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def setup(root, private_dir=None):
    root = Path(root).resolve()
    private = root / 'private'
    if private_dir:
        target = Path(private_dir).expanduser().resolve()
        if not target.is_dir():
            raise FileNotFoundError(f'Pasta externa indisponível: {target}')
        if private.is_symlink():
            if private.resolve() != target:
                raise RuntimeError('private/ já aponta para outro destino')
        elif private.exists():
            raise RuntimeError('private/ é uma pasta local; mova seu conteúdo antes de configurar o symlink')
        else:
            private.symlink_to(target, target_is_directory=True)
    assert_available(private)
    if not private.is_dir() or not private.is_symlink():
        raise RuntimeError('Configure private/ como symlink de uma pasta externa existente')
    receipts = []
    migration = private / 'data/runs/migration' / datetime.now().strftime('%Y-%m-%dT%H%M%S_%f')
    for name in ('raw', 'derived', 'runs'):
        source = root / 'data' / name
        destination = private / 'data' / name
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            if source.resolve() != destination.resolve():
                raise RuntimeError(f'Symlink aponta para outro destino: {source}')
            continue
        # Confere a árvore inteira antes de trocar o diretório local.
        entries = []
        if source.exists():
            for file in files(source):
                target = destination / file.relative_to(source)
                copy_verified(file, target)
                entries.append({'source': str(file.relative_to(root)),
                                'destination': str(target.relative_to(root)), 'sha256': sha256(file)})
            for item in entries:
                if sha256(root / item['destination']) != item['sha256']:
                    raise RuntimeError('Destino mudou durante a migração')
        source.parent.mkdir(parents=True, exist_ok=True)
        backup = root / '.runtime/archive-setup' / name
        if backup.exists():
            raise RuntimeError(f'Migração anterior requer revisão: {backup}')
        backup.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            source.rename(backup)
        try:
            source.symlink_to('../private/data/' + name, target_is_directory=True)
        except BaseException:
            if backup.exists():
                backup.rename(source)
            raise
        receipts.extend(entries)
        # Grava a prova de preservação antes de remover a cópia local.
        save_json(migration / 'data-links.json', receipts)
        if backup.exists():
            shutil.rmtree(backup)

    analysis = root / 'analysis'
    source_html = analysis / 'google-play-descritiva.html'
    source_assets = analysis / 'google-play-descritiva_files'
    output_link = analysis / 'output'
    legacy_output = analysis / 'google-play-descritiva-output'
    # Confere os links antigos antes de removê-los; nunca remove uma pasta local.
    if legacy_output.is_symlink():
        assert_available(legacy_output)
        if output_link.is_symlink() and output_link.resolve() != legacy_output.resolve():
            raise RuntimeError('Links de relatório apontam para destinos divergentes')
        for path in (source_html, source_assets):
            if (path.exists() or path.is_symlink()) and (
                    not path.is_symlink() or path.resolve() != (legacy_output / path.name).resolve()):
                raise RuntimeError(f'Atalho antigo divergente; migração cancelada: {path}')
        replace_link(output_link, os.readlink(legacy_output))
    elif legacy_output.exists():
        raise RuntimeError(f'Caminho antigo não é symlink: {legacy_output}')
    if source_html.is_file() and not source_html.is_symlink():
        if not source_assets.is_dir() or source_assets.is_symlink():
            raise RuntimeError('Recursos locais ausentes; preserve o relatório antes de migrar')
        candidate = private / 'reports/google-play-descritiva/2026-10-05'
        sources = [source_html, *files(source_assets)]
        if not all((candidate / file.relative_to(analysis)).is_file()
                   and sha256(file) == sha256(candidate / file.relative_to(analysis)) for file in sources):
            candidate = private / 'reports/google-play-descritiva' / datetime.now().strftime('imported-%Y-%m-%dT%H%M%S_%f')
        for file in sources:
            copy_verified(file, candidate / file.relative_to(analysis))
        copy_verified(analysis / 'google-play-descritiva.qmd', candidate / 'google-play-descritiva.qmd')
        replace_link(output_link, os.path.relpath(candidate, analysis))
        source_html.unlink()
        shutil.rmtree(source_assets)
    elif not output_link.exists():
        # Clone sem saída renderizada: aponta para a última versão completa disponível.
        versions = sorted((private / 'reports/google-play-descritiva').glob('*'))
        complete = [v for v in versions if (v / source_html.name).is_file()
                    and (v / source_assets.name).is_dir()]
        if complete:
            replace_link(output_link, os.path.relpath(complete[-1], analysis))
    if output_link.exists():
        old_links = [path for path in (source_html, source_assets) if path.is_symlink()]
        for path in old_links:
            if path.resolve() != (output_link / path.name).resolve():
                raise RuntimeError(f'Atalho antigo divergente; migração cancelada: {path}')
        for path in old_links:
            path.unlink()
        if legacy_output.is_symlink():
            legacy_output.unlink()
    app_link = analysis / 'app-store-output'
    if app_link.exists() and not app_link.is_symlink():
        raise RuntimeError(f'Caminho de saída existente não é symlink: {app_link}')
    if not app_link.exists():
        versions = sorted((private / 'reports/app-store-descritiva').glob('*'))
        complete = [v for v in versions if (v / 'app-store-descritiva.html').is_file()
                    and (v / 'app-store-descritiva_files').is_dir()]
        if complete:
            replace_link(app_link, os.path.relpath(complete[-1], analysis))
    print('Acervo configurado: data/{raw,derived,runs} aponta para private/data/.')
    print('Próximas coletas preparam os dados em .runtime/ e publicam no acervo externo.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', help='Pasta externa existente; configura private/ em um clone novo')
    args = parser.parse_args()
    setup(Path(__file__).resolve().parents[1], args.private_dir)


if __name__ == '__main__':
    main()
