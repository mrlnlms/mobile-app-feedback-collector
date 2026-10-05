"""Renderiza o relatório App Store no acervo externo.

Uso: venv/bin/python -m scripts.reports.render_app_store
"""

import argparse
from pathlib import Path

from scripts.reports.render_google_play import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    render(Path(__file__).resolve().parents[2], platform='app_store')


if __name__ == '__main__':
    main()
