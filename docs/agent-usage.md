# Operating the analyzer as an AI agent

This guide is for coding agents (Claude Code, Codex and similar) that operate the analyzer
for a person through the terminal. There is no MCP server or agent API: the CLI with
`--json` is the interface. Every example below runs offline with synthetic data.

Ground rules:

- Read `whatsapp-analyzer <command> --help` before using a command; do not invent flags.
- Always pass `--json` (before the command name) and read the single JSON document on stdout.
  Decide from `ok`, `error.code`, `error.details` and the exit code, never from the wording
  of messages, which is localized.
- Never pass `--authorize-send` on your own. Sending conversations to a provider is the
  person's decision, every time (see [Authorization](#authorization)).
- Never write an API key into a file, a command line or a message. Ask the person to export
  it in their own shell.
- Never lift budgets or limits to get a run through; report the stop reason instead.
- Do not edit `analyzer.toml`, `models.toml` or `pricing.toml` unless the person asked for
  that change.

## 1. Identify the project

The repository root has `AGENTS.md`, `README.md` and `pyproject.toml` (package
`whatsapp-conversation-analyzer`, command `whatsapp-analyzer`). The documentation is in
`docs/`: [providers.md](providers.md), [runner.md](runner.md), [llm.md](llm.md),
[metrics.md](metrics.md), [input-formats.md](input-formats.md).

## 2. Install

```bash
uv sync                                   # development checkout (both provider SDKs included)
pip install 'whatsapp-conversation-analyzer[anthropic]'   # or [openai] / [providers]
```

## 3. Check the environment

```bash
whatsapp-analyzer --json doctor
```

`data.checks[]` has one entry per check (`status`: `ok`, `info` or `fail`). Exit code 9
means a required check failed. `provider_sdks` lists the installed SDKs and `credentials`
says which default key variables are set, never their values.

## 4. Prepare a workspace

```bash
whatsapp-analyzer --json init ./ws
```

The workspace holds `analyzer.toml` and `analyzer.db` (which contains the messages: treat it
as private). It is created with a `.gitignore` that excludes everything.

## 5. Accepted formats

WhatsApp TXT exports (Android and iOS, several locales), CSV and XLSX with a column mapping.
See [input-formats.md](input-formats.md) and `whatsapp-analyzer import --help`. For a demo,
generate synthetic data (no real data is involved):

```bash
whatsapp-analyzer --json demo-data ./demo --conversations 4 --seed 7
```

The generator always adds two edge-case conversations, so `data.conversations` is 6 here.

## 6. Import

```bash
whatsapp-analyzer --json import demo/conversations.csv -w ./ws
```

Exit code 3 means the input failed validation; `data` lists issues with codes and line
numbers, never message text. If a real export is involved, ask the person before reading it.

## 7. Validate

The import validates as it goes. Check the workspace afterwards:

```bash
whatsapp-analyzer --json status -w ./ws
```

`data.conversations.total` and `data.messages_current` confirm what was stored.

## 8. Metrics

```bash
whatsapp-analyzer --json metrics run -w ./ws
whatsapp-analyzer --json export -w ./ws --out scores.csv
```

Deterministic, free and offline. See [metrics.md](metrics.md) for each metric.

## 9. Configure a provider

With the person, choose the provider and model. Without a choice, keep `provider = "fake"`
(offline, free). Write the configuration only when asked, for example in a separate file:

```toml
# real.toml
[llm]
provider = "anthropic"
model = "claude-haiku-5-5"

[llm.connection]
api_key_env = "MY_ANTHROPIC_KEY"
```

Pass it with `llm run --config real.toml`, or put the same sections in the workspace's
`analyzer.toml`. `--config` replaces the whole workspace configuration for that command:
sections the file leaves out (`[metrics]`, `[llm.context]`, `[llm.limits]`, ...) take their
documented defaults. All settings are in [providers.md](providers.md#configure).

## 10. The person's own key

Explain that the person needs their own key from the provider's console, and that this tool
is free: the provider bills them directly. Ask them to export it in the shell that runs the
analyzer (you never see or store it):

```bash
export MY_ANTHROPIC_KEY="..."     # typed by the person
```

If it is missing, `llm run` exits with code 4 and `error.code = "API_KEY_MISSING"`;
`error.details.env` names the variable to export. A local server that needs no key still
needs the configured variable to be set (any value, for example `export LOCAL_LLM_KEY=none`),
so that no default key variable is ever read by accident.

## 11. Custom models and endpoints

A model that is not in the built-in catalog works; declare its limits in
`<workspace>/models.toml` and its price in `<workspace>/pricing.toml`
([providers.md](providers.md#models-an-open-catalog)). An OpenAI-compatible service needs
`provider = "openai-compatible"` and `base_url`. Run notices (`data.notices`) say what is
unknown: `MODEL_NOT_IN_CATALOG`, `STRUCTURED_OUTPUT_UNDECLARED`, `CONTEXT_WINDOW_UNKNOWN`,
`PRICE_MISSING`, `PRICE_STALE`.

## 12. Preview

```bash
whatsapp-analyzer --json llm preview chat_0001 -w ./ws              # counts and estimates
whatsapp-analyzer --json llm preview chat_0001 -w ./ws --simulate   # offline fake answer
```

The preview never contacts a provider and shows no message text unless `--show-content`
(and a confirmation) is given.

## 13. Estimated consumption

Run `llm run` without `--authorize-send`. With a real provider it sends nothing, exits with
code 12 and reports what it would send:

```bash
whatsapp-analyzer --json llm run -w ./ws --config real.toml --max-cost 0.05
```

```json
{"ok": false, "command": "llm run", "error": {
  "code": "AUTHORIZATION_REQUIRED", "exit_code": 12,
  "details": {
    "destination": "anthropic", "model": "claude-haiku-5-5", "loopback": false,
    "authorize_with": ["--authorize-send", "anthropic"],
    "notice": ["The prepared text of each selected conversation ...", "Masking is best-effort ..."],
    "estimate": {"jobs": 6, "requests": 6, "max_requests": 18,
                 "estimated_input_tokens": 7471, "max_output_tokens_per_request": 300,
                 "worst_case_tokens": 27813, "worst_case_cost": "0.0139065", "currency": "USD"}}}}
```

`details` also carries `run_id` (the run is planned locally and left `interrupted` with
`stop_reason: "AUTHORIZATION_REQUIRED"`, ready to resume) and `notices`, the same codes a
run reports in `data.notices` (`MODEL_NOT_IN_CATALOG`, `PRICE_MISSING`, ...): show them too.
`worst_case_cost` assumes every attempt uses its maximum (`null` when the model has no
price); real costs are usually far lower and the run's limits (`--max-cost`, `--max-tokens`)
still apply.

## 14. Authorization

Show the person `details.destination`, `details.model`, `details.estimate` and every line of
`details.notice`, and ask whether they authorize sending these conversations to that
destination. Only after an explicit yes, for this destination, run again with the exact
values of `details.authorize_with`:

```bash
whatsapp-analyzer --json llm run -w ./ws --config real.toml --max-cost 0.05 --authorize-send anthropic
```

The authorization covers that one invocation. Resuming, `--retry-failed` and
`--resend-unknown` ask again (exit 12), and you must ask the person again. A wrong identity
gives `AUTHORIZATION_MISMATCH` and plans nothing.

## 15. Run

The offline demo needs no authorization:

```bash
whatsapp-analyzer --json llm run -w ./ws
```

`data.status` is `completed`, `completed_with_failures`, `budget_reached`, `paused` or
`interrupted` (`data.run.stop_reason` says why: `CRASH`, `AUTHORIZATION_REQUIRED`, a budget
or an account error); `data.jobs` counts jobs by state; `data.destination` and
`data.authorization` (`null`, `flag` or `interactive`) say where content went and why it was
allowed. The destination is the provider name, plus the endpoint when a `base_url` is
configured (`openai-compatible@https://host:443/path`).

In `attempts.by_billing`, `billed` means a response came back and its usage applies; with
the fake provider it costs 0. The fake provider reports no usage of its own:
`usage.reported` is `null` and `usage.estimated` holds local estimates. Add `--events events.ndjson` to follow progress: one JSON object per line, with ids
and codes only.

## 16. Status

```bash
whatsapp-analyzer --json llm status -w ./ws
```

Jobs by state, reported and estimated tokens, known cost, reservations and the budget.
`stale: true` means a run was left running by a crashed process; the next `llm run` recovers it.

## 17. Errors

| Exit | Meaning | What to do |
|---:|---|---|
| 0 | completed | report the results |
| 2 | invalid usage | read `--help`; fix the command |
| 3 | input validation failed | report the issues; ask before touching real files |
| 4 | invalid configuration (`error.code`: `API_KEY_MISSING`, `TEMPERATURE_UNSUPPORTED`, `BASE_URL_INSECURE`, `OPTION_UNSUPPORTED`, `PROVIDER_SDK_MISSING`, `BUDGET_UNVERIFIABLE`, ...) | show `error.message` and `error.hint`; ask the person before changing settings |
| 5 | run paused by a provider account error (key, credits, model access) | report `data.run.stop_reason`; the person must fix the account; then resume |
| 6 | interrupted | run the same command again to resume |
| 7 | budget reached | report it; the run resumes with a higher limit, which only the person can decide |
| 8 | finished with failed jobs or jobs awaiting reconciliation | see below |
| 12 | authorization required, declined or mismatched | steps 13 and 14 |
| 1 | unexpected internal error | report it; `WCA_DEBUG=1` shows the traceback |
| 9 | an environment check failed (`doctor`) | report the failed check |
| 10 | reserved for calibration (milestone 8); not used yet | - |
| 11 | an output file could not be written | report the path and reason |

`error.details` carries machine-readable facts (variable names, limits, the exact flag),
never keys or message text.

## 18. Resume and reconcile

Run the same `llm run` command again: completed jobs are kept, pending ones continue, and
identical requests are reused. Jobs `awaiting_reconciliation` had an unknown outcome (for
example a timeout after sending) and may already have been billed. They are never resent
automatically; resending needs `--resend-unknown` and a new authorization, and you must tell
the person that it may be billed twice. `--retry-failed` sends failed jobs again.

With an OpenAI-compatible endpoint or a custom `base_url`, every HTTP error (even 400 or 429)
is `ENDPOINT_ERROR`: a gateway may answer an error after the model ran, so such jobs await
reconciliation too, are never retried automatically, and an account problem (a wrong key)
does not pause the run. `llm export` shows `error_kind` per job (the HTTP status of each
attempt is kept in the workspace's `llm_attempts` table); tell the person before suggesting
`--resend-unknown`. Details in
[runner.md](runner.md).

## 19. Export

```bash
whatsapp-analyzer --json llm export -w ./ws --out llm.csv
```

One row per conversation of the run, with the label, reasons, evidence message ids,
statuses, tokens and cost, and a `llm.csv.meta.json` describing the run. `data.partial` and
`data.awaiting_reconciliation_rows` say whether the export is complete.

## 20. Explain the results

Explain labels as model judgments about observable conversation behavior (`HIGH`, `MEDIUM`,
`LOW`, `REVIEW_REQUIRED`), with their reason codes, next to the deterministic metric levels.
Say what is estimated (tokens, costs) and what was reported by the provider. Never present
masking as anonymization, and never quote conversation text unless the person asks for it.

## Complete offline example

```bash
whatsapp-analyzer --json demo-data ./demo --conversations 4 --seed 7
whatsapp-analyzer --json init ./ws
whatsapp-analyzer --json import demo/conversations.csv -w ./ws
whatsapp-analyzer --json metrics run -w ./ws
whatsapp-analyzer --json llm run -w ./ws
whatsapp-analyzer --json llm status -w ./ws
whatsapp-analyzer --json llm export -w ./ws --out llm.csv
```

Each command prints one JSON document and exits with code 0.
