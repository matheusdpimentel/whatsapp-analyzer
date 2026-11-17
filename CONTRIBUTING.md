# Contributing

Thanks for helping. The project is free, local-first and privacy-sensitive; a few rules keep
it that way.

## Ground rules

- **Never** include real conversations, names, phone numbers, workspaces or keys in code,
  tests, issues or pull requests. Use `whatsapp-analyzer demo-data` or invented data
  (phones in the reserved `555-01XX` range, addresses at `example.com`/`example.org`).
  `tests/test_repository_hygiene.py` fails on real-looking data.
- Tests are offline (sockets are blocked) and never call a paid provider. Live provider
  tests are opt-in and described in [tests/live/README.md](tests/live/README.md).
- Tests should not share definitions with the code they check (independent fixtures and
  expectations).
- Code, messages, docs and commits are in English; the CLI also ships a pt-BR catalog with
  the same keys.

## Setup and checks

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy
uv run python scripts/check_quickstart.py "$(uv run which whatsapp-analyzer)"   # full workflow
```

Documented commands are tested: if you rename a command or option, update the docs, or
`tests/test_documented_commands.py` fails.

## Pull requests

Keep changes focused, explain the why, and update docs and `CHANGELOG.md`. Changes to
exports, exit codes or the workspace schema are compatibility changes: call them out.
Architecture decisions go in [docs/adr/](docs/adr/).
