# Repository Guidelines

## Project Structure & Module Organization

`scripts/collect/` contains the Google Play and App Store collectors and storage helpers. `scripts/reports/` publishes Quarto reports; `scripts/research/` holds topic-specific searches. Source-specific operating guides live in `docs/`, report sources in `analysis/*.qmd`, and automated tests in `tests/`. `config.yaml` defines banks, app IDs, dates, and collection settings. `data/README.md` documents the archive; `data/manifest.json` records its versioned inventory.

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

Use four-space indentation and Python `snake_case`. Keep Google Play and App Store separate where pagination, coverage, timestamps, state, or recovery differ. Use `pathlib.Path` for paths, following the existing code. No formatter or linter is configured; keep changes consistent with surrounding files. Name tests `tests/test_*.py` and methods `test_*`.

## Testing Guidelines

Tests use standard-library `unittest` and temporary files with mocks for collection and publication behavior. Run `venv/bin/python -m unittest discover -s tests` before proposing changes. Preserve existing failure tests and add focused tests when changing collection, publication, deduplication, checkpoints, snapshots, state, or recovery.

## Commit & Pull Request Guidelines

Recent commits use short area-prefixed subjects, such as `feat:`, `refactor:`, `docs:`, or `analysis:`. In pull requests, describe the affected platform or report, archive impact, and validation performed.

## Work Plans & Handoffs

When using the `writing-plans` skill, save plans under `private/workstreams/plans/<workstream>/`, not `docs/superpowers/plans/`. Keep plans for the same workstream together. The App Store AMP/Web investigation and collection enablement plans belong to `private/workstreams/plans/app-store-amp-web-collection/`. Plans in `private/` remain outside Git; commit the project instructions, code, tests, and public documentation separately.

Save session handoffs and investigation checkpoints under `private/workstreams/handoffs/<workstream>/`, not in the versioned `docs/` tree. The Inter AMP/Web handoff lives in `private/workstreams/handoffs/app-store-amp-web-collection/inter-handoff.md`. Keep `docs/` focused on durable repository contracts and operating guides; handoffs in `private/` remain outside Git.

## Archive & Privacy

`data/raw/<platform>/<bank>/reviews_raw.parquet` holds the current canonical bases. Publish through the existing `scripts/collect/*_storage.py` helpers: stage locally, validate, deduplicate by `id_review`, verify hashes, snapshot the previous base, and replace safely. Never write directly to a canonical Parquet.

`private/` links to the external archive; `data/{raw,derived,runs}/` accesses it. Keep collector staging, checkpoints, locks, and unfinished publication artifacts in local `.runtime/`, outside the official archive. An unavailable archive symlink must fail, never appear to be an empty base or first collection. Use `scripts/probe/` for isolated experiments before promoting behavior to collectors; probes must preserve canonical bases. Do not commit raw reviews, Parquets, or private research. Consult `data/README.md` before changing archive paths.
