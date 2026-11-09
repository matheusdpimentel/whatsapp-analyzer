# WhatsApp Conversation Analyzer

A local, terminal-based tool for processing, analyzing and classifying WhatsApp conversation
exports. It computes deterministic engagement metrics and can optionally classify conversations
with an LLM provider that you configure.

> **Status: early development (milestone 4).** Conversations can be imported, validated and
> stored in a local workspace, and deterministic engagement metrics can be computed, compared
> and exported. The offline LLM core (prompts, context preparation, validation, token and cost
> accounting) can be previewed locally. Real providers come in later milestones. Architecture decisions are recorded in
> [`docs/adr/`](docs/adr/).

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

### Importing conversations

```bash
whatsapp-analyzer import ./exports                                # validate and report
whatsapp-analyzer import ./exports --normalized-out messages.csv   # also write canonical CSV
whatsapp-analyzer import chat.txt --owner "Your Name" --date-order dmy   # WhatsApp TXT export
whatsapp-analyzer import data.csv --mapping my-mapping.toml        # CSV/XLSX with other columns
```

Supported inputs: iMazing TXT and CSV exports, WhatsApp's own TXT exports (experimental), and
any CSV/XLSX file with a column mapping. Every import prints a validation report with counts
and finding codes, never message text. Details, the canonical schema, mapping files and
troubleshooting are in [`docs/input-formats.md`](docs/input-formats.md).

### Engagement metrics

```bash
whatsapp-analyzer init ./analysis                          # workspace: analyzer.toml + analyzer.db
whatsapp-analyzer import ./exports --workspace ./analysis  # store conversations (versioned)
whatsapp-analyzer metrics run -w ./analysis                # frequency, reciprocity, initiative, recency
whatsapp-analyzer metrics compare 1 2 -w ./analysis        # compare two scoring configurations
whatsapp-analyzer status -w ./analysis
whatsapp-analyzer export -w ./analysis --out scores.csv    # + scores.csv.meta.json
```

The metrics describe observable activity in a conversation, never interest, sentiment or
personal attributes. They are deterministic and computed without any LLM. Levels are `HIGH`,
`MEDIUM`, `LOW`, `INSUFFICIENT_DATA` and `NOT_COMPARABLE` (group chats). Raw metrics are stored
apart from scores, so changing weights or thresholds re-scores without recomputing them.
Re-importing unchanged conversations does nothing, and earlier results remain traceable to the
data version they were computed from. Definitions, configuration, caching rules and the export
format are in [`docs/metrics.md`](docs/metrics.md).

### LLM core (offline preview)

```bash
whatsapp-analyzer llm prompts                          # prompt templates, versions, hashes
whatsapp-analyzer llm preview KEY -w ./analysis        # what would be sent: counts, estimates
whatsapp-analyzer llm preview KEY -w ./analysis --simulate   # run the offline fake provider
```

Nothing is sent to any provider yet. The preview shows no message content unless you ask for it
with `--show-content` or `--out` and confirm. Identifiers are masked, but masking is
pseudonymization, not anonymization. See [`docs/llm.md`](docs/llm.md).

### Synthetic demo data

You can try the tool without real conversations:

```bash
whatsapp-analyzer demo-data ./demo                              # English, Android and iOS exports
whatsapp-analyzer demo-data ./demo-pt --locale pt-BR --seed 42  # Brazilian Portuguese
```

This writes WhatsApp chat exports (`txt/<format>/*.txt`), the same messages as CSV and XLSX,
and a `manifest.json`. The same seed always produces the same files. All names, phone numbers
(555-01XX) and addresses (example.com) are fictional. Ready-made datasets are in
[`examples/`](examples/).

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
| 11 | Output could not be written (permissions, invalid path, full disk) |
| 130 | Interrupted by the user (an interrupted `metrics run` resumes when run again) |

Codes 5 to 8 and 10 are reserved for commands that are not implemented yet. Code 4 covers
invalid `analyzer.toml` files and missing or incompatible workspaces.

## Development

```bash
uv run pytest            # tests (network access is blocked)
uv run ruff check        # lint
uv run ruff format       # format
uv run mypy              # type check (strict)
```

## Privacy

The analyzer runs locally and sends no data anywhere. Reports show counts and codes, never
message text; file names are hidden unless `--show-file-names` is given. `doctor` only reports
whether provider credentials are set, never their values. A normalized CSV contains your
messages: keep it as private as the original exports. The same applies to a workspace
(`analyzer.db` stores the messages) and to score exports, which contain conversation keys.
Workspaces are created readable only by you and with a `.gitignore` that excludes everything. Keep API keys in environment variables or a local
`.env` file (see `.env.example`); never pass them as command-line arguments.

## License

[MIT](LICENSE)
