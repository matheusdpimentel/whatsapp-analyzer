# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[PEP 440](https://peps.python.org/pep-0440/) (`1.0.0b1` is a beta of `1.0.0`).

## [1.0.0b1] - unreleased

First public beta. No new features compared with milestones M0–M8; this release prepares
distribution, documentation and verification.

### Added
- Release tooling: `scripts/check_quickstart.py` (full workflow on synthetic data against an
  installed package), `scripts/benchmark.py`, `docs/releasing.md`, `docs/benchmarks.md`.
- Tests that run every documented command/option check and guard against real or personal
  data in the repository (`tests/test_documented_commands.py`,
  `tests/test_repository_hygiene.py`).
- `SECURITY.md`, `CONTRIBUTING.md`, issue and pull request templates, a release workflow
  that builds and verifies the package (it does not publish).

### Changed
- Version `1.0.0b1`, classifier Beta; README rewritten for installation and quickstart.

### Compatibility
- Export formats (`*.csv` + `.meta.json`), exit codes, `--json` schema 1 and the workspace
  schema are unchanged from M8.

### Known limitations
- WhatsApp's own TXT export is **experimental** until validated with real exports.
- Group chats are `NOT_COMPARABLE` in the metrics.
- Masking before sending to a provider is best-effort pseudonymization, not anonymization.

## Earlier milestones

M0 skeleton and CLI; M1 synthetic data; M2 import and validation; M3 workspace and metrics;
M4 offline LLM core; M5 persistent runner; M6 real providers (BYOK); M7 independent review;
M8 calibration. See [docs/roadmap.md](docs/roadmap.md) and [docs/adr/](docs/adr/).
