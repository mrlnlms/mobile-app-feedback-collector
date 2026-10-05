# Repository Guidelines

## Project Structure & Module Organization

`scripts/collect/` contains the Google Play and App Store collectors and storage helpers. `scripts/reports/` publishes Quarto reports; `scripts/research/` holds topic-specific searches; `scripts/probe/` and `scripts/history/` contain experiments and the original proof of concept. Source-specific operating guides live in `docs/`, report sources in `analysis/*.qmd`, and automated tests in `tests/`. `config.yaml` defines banks, app IDs, dates, and collection settings. `data/README.md` documents the archive; `data/manifest.json` records its versioned inventory.

## Build, Test, and Development Commands

Run commands from the repository root. There is no build step for the Python code.

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m unittest discover -s tests
venv/bin/python -m scripts.collect.google_play --all --dry-run
venv/bin/python -m scripts.collect.google_play --bank nubank
venv/bin/python -m scripts.collect.app_store --bank nubank
```

The dry run reports Google Play collection boundaries without fetching or writing. Collector commands fetch and publish archive data; read the matching `docs/` guide first. To render reports, install `analysis/requirements.txt` and Quarto, then run `venv/bin/python -m scripts.reports.render_google_play` or `render_app_store`.

## Coding Style & Naming Conventions

Use four-space indentation and Python `snake_case` for modules, functions, variables, and test methods. Keep platform-specific behavior in its corresponding module and use `pathlib.Path` for paths, following the existing code. No formatter or linter is configured; keep changes consistent with surrounding files. Name tests `tests/test_*.py` and methods `test_*`.

## Testing Guidelines

Tests use standard-library `unittest` and temporary files with mocks for collection and publication behavior. Run `venv/bin/python -m unittest discover -s tests` before proposing changes. Add focused tests when changing collection boundaries, ID deduplication, archive publication, recovery, or report links. No coverage threshold is configured.

## Commit & Pull Request Guidelines

Recent commits use short, imperative subjects prefixed by area, such as `feat:`, `refactor:`, `docs:`, or `analysis:`. In pull requests, describe the affected platform or report, data or archive impact, and validation performed. Link a related issue when one exists; include screenshots for visible report changes.

## Archive & Privacy

`private/` points to an external local archive. `data/raw/`, `data/derived/`, `data/runs/`, `.runtime/`, and rendered report outputs are local or linked data, not source files. Do not commit raw reviews, Parquet files, or research material from `private/`. Preserve symlinks and consult `data/README.md` before changing archive paths.
