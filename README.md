# WhatsApp Conversation Analyzer

A local, terminal-based tool for processing, analyzing and classifying WhatsApp conversation
exports. It computes deterministic engagement metrics and can optionally classify conversations
with an LLM provider that you configure.

> **Status: early development (milestone 6).** Conversations can be imported, validated and
> stored in a local workspace; deterministic engagement metrics can be computed, compared and
> exported; and LLM classification runs as a persistent, resumable, budgeted job with the
> offline fake provider or a real provider (OpenAI, Anthropic or an OpenAI-compatible
> endpoint) using your own key. See [`docs/roadmap.md`](docs/roadmap.md); architecture
> decisions are recorded in [`docs/adr/`](docs/adr/).

The tool is free and open source. There is no backend, account or subscription: with a real
provider you bring your own key (BYOK) and the provider bills you directly. AI agents can
operate it through the CLI: see [`AGENTS.md`](AGENTS.md) and
[`docs/agent-usage.md`](docs/agent-usage.md).

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

### LLM classification

```bash
whatsapp-analyzer llm prompts                          # prompt templates, versions, hashes
whatsapp-analyzer llm preview KEY -w ./analysis        # what would be sent: counts, estimates
whatsapp-analyzer llm preview KEY -w ./analysis --simulate   # run the offline fake provider
```

```bash
whatsapp-analyzer llm run -w ./analysis --max-tokens 200000   # checkpointed, resumable run
whatsapp-analyzer llm status -w ./analysis                    # jobs, billing, tokens, costs
whatsapp-analyzer llm export -w ./analysis --out llm.csv      # partial-aware export
```

Runs checkpoint every attempt before calling the provider, resume after Ctrl+C or a crash,
reuse earlier results, never resend an attempt whose outcome is unknown without
`--resend-unknown`, and stop before exceeding token or cost budgets. See
[`docs/runner.md`](docs/runner.md).

The default provider is an offline fake: nothing leaves the machine. To use a real provider,
install its SDK and point the configuration at the environment variable that holds your key:

```bash
pip install 'whatsapp-conversation-analyzer[anthropic]'    # or [openai], [providers]
export ANTHROPIC_API_KEY="your-own-key"
```

```toml
# analyzer.toml
[llm]
provider = "anthropic"            # openai | anthropic | openai-compatible | fake
model = "claude-haiku-5-5"        # any model id; known limits are in the model catalog
[llm.connection]
api_key_env = "ANTHROPIC_API_KEY" # the variable's NAME, never the key
```

```bash
whatsapp-analyzer llm run -w ./analysis --max-cost 1.00             # shows what would be sent, asks
whatsapp-analyzer --json llm run -w ./analysis --authorize-send anthropic   # non-interactive
```

Every run that may send content needs explicit authorization for that invocation: typed in
a terminal, or `--authorize-send` with the exact destination. Without it nothing is sent and
the command exits with code 12, reporting the estimate. Masking is best-effort
pseudonymization, not anonymization. The preview shows no message content unless you ask
for it with `--show-content` or `--out` and confirm. See
[`docs/providers.md`](docs/providers.md) (configuration, models, endpoints, costs),
[`docs/llm.md`](docs/llm.md) and
[`docs/adding-a-provider.md`](docs/adding-a-provider.md) to support another provider.

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
| 6 | Run interrupted (Ctrl+C or a crash); it can be resumed |
| 7 | Budget limit reached |
| 8 | Run finished with failed items or items awaiting reconciliation |
| 9 | Environment check failed |
| 10 | Calibration required before a full LLM run |
| 11 | Output could not be written (permissions, invalid path, full disk) |
| 12 | Sending content to a provider needs explicit authorization (`--authorize-send`) |
| 130 | Interrupted by the user (an interrupted `metrics run` resumes when run again) |

Codes 5 to 8 and 12 are used by `llm run` (see [`docs/runner.md`](docs/runner.md) and
[`docs/providers.md`](docs/providers.md)); code 10 is reserved for a later milestone. Code 4
covers invalid `analyzer.toml` files, missing or incompatible workspaces and provider
configuration errors (missing key, unsupported option, invalid endpoint, ...).

## Development

```bash
uv run pytest            # tests (network access is blocked; no paid calls, see tests/live/)
uv run ruff check        # lint
uv run ruff format       # format
uv run mypy              # type check (strict)
```

## Privacy

The analyzer runs locally and has no telemetry. Conversation content is sent only to the LLM
provider you configure, directly from your machine, and only after you authorize it for
that run. Reports show counts and codes, never
message text; file names are hidden unless `--show-file-names` is given. `doctor` only reports
whether provider credentials are set, never their values. A normalized CSV contains your
messages: keep it as private as the original exports. The same applies to a workspace
(`analyzer.db` stores the messages) and to score exports, which contain conversation keys.
Workspaces are created readable only by you and with a `.gitignore` that excludes everything. Keep API keys in environment variables (see
`.env.example`); the configuration names the variable, and the key is never written to the
workspace, logs or exports. Never pass keys as command-line arguments.

## License

[MIT](LICENSE)
