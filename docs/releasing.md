# Releasing

Maintainer checklist. Publishing (remote repository, tags, PyPI) is always a manual step that
needs the owner's approval; nothing in this repository publishes automatically.

## Versioning

[PEP 440](https://peps.python.org/pep-0440/): `1.0.0b1` (beta) → `1.0.0` (stable). The version
lives only in `pyproject.toml`; `whatsapp-analyzer --version` reads the installed metadata.
Before `1.0.0`, the roadmap's final validation must pass (see below).

## Verify locally

```bash
uv sync --locked
uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy
rm -rf dist && uv build
```

Then check the **installed** package, outside the checkout (this is what users get):

```bash
uv venv /tmp/wca-venv -p 3.12
uv pip install -p /tmp/wca-venv/bin/python dist/*.whl
cd "$(mktemp -d)"
python /path/to/checkout/scripts/check_quickstart.py /tmp/wca-venv/bin/whatsapp-analyzer
```

The script runs the documented workflow on synthetic data with the offline fake provider
(import, metrics, LLM run, review, consolidation, calibration, exports). CI repeats it on
Linux, macOS and Windows (`package` job). Optional extras: install
`'dist/<wheel>[providers]'` and run `whatsapp-analyzer doctor`.

## Security checks

```bash
uv export --no-dev --all-extras --no-hashes > /tmp/req.txt
uvx pip-audit -r /tmp/req.txt --no-deps --disable-pip      # dependency vulnerabilities
uv run pytest tests/test_repository_hygiene.py             # no real/personal data tracked
```

Inspect the sdist and wheel contents (`tar tzf`, `unzip -l`): only source, tests, docs,
synthetic examples and package data. Review `git log --format='%an <%ae>'`: author metadata
becomes public with the repository.

## Publishing (manual, owner-approved)

1. Update `CHANGELOG.md` (move *unreleased* to the release date).
2. Commit, then tag `vX.Y.Z...` (the release workflow checks the tag equals the version and
   attaches the built `dist/`; it does not publish).
3. Create the GitHub repository/release from the tag.
4. Publish to PyPI with trusted publishing or an API token kept in your own environment:
   `uv publish`. Try TestPyPI first for a beta. The name `whatsapp-conversation-analyzer`
   was unclaimed on PyPI when checked (read-only lookup, 2026-11-16); claiming happens only at
   the first publish.
5. After publishing, verify from a clean machine: `uvx --from 'whatsapp-conversation-analyzer==1.0.0b1' whatsapp-analyzer --version`
   (the package name differs from the command, so `--from` is required; the exact pin lets a beta resolve).
   Before publishing, the same command works against the local build by adding `--find-links dist`.

## Before `1.0.0` (stable)

- Beta feedback triaged; no open compatibility or privacy issue.
- Decide whether WhatsApp's TXT export is still *experimental* (it stays so until validated
  with real exports, which are never added to the repository).
