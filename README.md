# WhatsApp Conversation Analyzer

A local, terminal-based tool for processing, analyzing and classifying WhatsApp conversation
exports. It computes deterministic engagement metrics and can optionally classify conversations
with an LLM provider that you configure.

> **Status: early development (milestone 0).** Only the project skeleton and the `doctor`
> command exist so far. Architecture decisions are recorded in [`docs/adr/`](docs/adr/).

This project is not affiliated with, endorsed by, or sponsored by WhatsApp or Meta.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) for development

## Installation (development)

```bash
git clone <repository-url>
cd whatsapp-conversation-analyzer
uv sync
uv run whatsapp-analyzer --help
```

## Usage

```bash
whatsapp-analyzer doctor                  # check the local environment
whatsapp-analyzer --json doctor           # same, as one JSON document on stdout
whatsapp-analyzer --lang pt-BR doctor     # Brazilian Portuguese interface
```

Global options go before the command name. The interface language can also be set with the
`WCA_LANG` environment variable (`en` or `pt-BR`).

### Machine-readable output

With `--json`, every command writes exactly one JSON document to stdout:

```json
{"schema": 1, "ok": true, "command": "doctor", "data": {"checks": [...], "summary": {...}}}
```

Failures add an `error` object with a stable English `code`, a localized `message` and the
`exit_code`. `--help` and `--version` are meant for people and always print plain text.

### Exit codes

| Code | Meaning |
|-----:|---------|
| 0 | Success |
| 1 | Unexpected internal error (set `WCA_DEBUG=1` to see the traceback) |
| 2 | Invalid usage |
| 3 | Input validation failed |
| 4 | Invalid configuration |
| 5 | Provider account error (credentials, billing, quota, model access) |
| 6 | Run paused or interrupted; it can be resumed |
| 7 | Budget limit reached |
| 8 | Run finished with failed items |
| 9 | Environment check failed |
| 10 | Calibration required before a full LLM run |
| 130 | Interrupted by the user |

Codes 3 to 8 and 10 are reserved for commands that are not implemented yet.

## Development

```bash
uv run pytest            # tests (network access is blocked)
uv run ruff check        # lint
uv run ruff format       # format
uv run mypy              # type check (strict)
```

## Privacy

The analyzer runs locally. `doctor` sends no data anywhere and only reports whether provider
credentials are set, never their values. Keep API keys in environment variables or a local
`.env` file (see `.env.example`); never pass them as command-line arguments.

## License

[MIT](LICENSE)
