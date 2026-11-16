# Agent instructions

WhatsApp Conversation Analyzer: a free, local-first CLI (`whatsapp-analyzer`) that imports
WhatsApp exports, computes deterministic engagement metrics and optionally classifies
conversations with an LLM provider the user configures with their own key.

## Where things are

- [README.md](README.md): install, commands, exit codes, privacy.
- [docs/agent-usage.md](docs/agent-usage.md): how to operate the CLI step by step as an agent.
- [docs/providers.md](docs/providers.md): providers, keys, models, endpoints, costs, errors.
- [docs/adding-a-provider.md](docs/adding-a-provider.md): writing a new adapter.
- [docs/review.md](docs/review.md): independent review by a second model and consolidation.
- [docs/calibration.md](docs/calibration.md): human-reviewed examples, agreement reports, run comparison.
- [docs/runner.md](docs/runner.md), [docs/llm.md](docs/llm.md), [docs/metrics.md](docs/metrics.md),
  [docs/input-formats.md](docs/input-formats.md), [docs/roadmap.md](docs/roadmap.md),
  [docs/adr/](docs/adr/).

## Rules

- Run `whatsapp-analyzer <command> --help` and read the docs above before running a command.
  Do not invent flags. Use `--json` and decide from `error.code`, `error.details` and exit
  codes.
- Never pass `--authorize-send` without the user's explicit approval for that destination,
  for that invocation. Exit code 12 means: show the user `error.details` and ask.
- Never bypass or raise budgets, rate limits or reconciliation (`--resend-unknown` may bill
  twice) without the user asking for it.
- Never write, print or commit API keys. Users export them in their own shell; configuration
  holds only `api_key_env`.
- Never commit real conversations, workspaces (`analyzer.db`), exports or `.env` files. Use
  `whatsapp-analyzer demo-data` for synthetic data. Ask before reading a real export.
- Do not change `analyzer.toml`, `models.toml` or `pricing.toml` unless asked. `calibrate suggest`
  is exploratory and applies nothing; never apply its suggestions unasked.
- `calibrate show` prints real message text on the terminal for the person labeling; do not
  copy that text into files, commits or prompts.

## Development

```bash
uv sync
uv run pytest                     # offline: sockets are blocked, no paid calls
uv run ruff check && uv run ruff format --check
uv run mypy                       # strict
```

Code, CLI messages (English catalog), tests, docs and commit messages are in English; the
interface also ships a Brazilian Portuguese catalog with the same keys.
