"""Renderiza qualquer QMD de analysis/ no padrão de publicação do projeto.

Temporário e mantido em analysis/ de propósito (o foco do projeto é o coletor).
Reutiliza `scripts.reports.render_google_play.render` sem alterá-lo: cópia local,
conferência por hash, versão em private/reports/<nome>/<data-hora>/ e atalho
analysis/<nome>-output. Os relatórios já existentes mantêm seus atalhos.

Uso: venv/bin/python analysis/render.py analysis/google-play-descritiva-v2.qmd
"""
import argparse
import os
import sys
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parent
ROOT = ANALYSIS.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.reports import render_google_play as report  # noqa: E402


def render_qmd(qmd, root=ROOT):
    """Publica um QMD de analysis/ e devolve a pasta da versão criada."""
    root = Path(root).resolve()
    stem = Path(qmd).stem
    if not (root / "analysis" / f"{stem}.qmd").is_file():
        raise FileNotFoundError(f"QMD não encontrado em analysis/: {stem}.qmd")

    platform = next((name for name, entry in report.REPORTS.items() if entry[0] == stem), None)
    registered = platform is not None
    if not registered:
        # Sem plataforma de dados: o nome não aponta para nenhuma comparação de coleta.
        platform = stem
        report.REPORTS[platform] = (stem, f"{stem}-output", stem)

    # O render roda numa cópia temporária; analysis/ precisa estar no path dos módulos auxiliares.
    previous = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root / "analysis"), previous)))
    try:
        return report.render(root, platform=platform)
    finally:
        if previous is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous
        if not registered:
            report.REPORTS.pop(platform, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qmd", help="caminho ou nome de um QMD dentro de analysis/")
    render_qmd(parser.parse_args().qmd)


if __name__ == "__main__":
    main()
