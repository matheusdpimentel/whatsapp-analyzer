# Security policy

WhatsApp Conversation Analyzer runs locally and processes private conversations, so privacy
and security reports are taken seriously.

## Supported versions

The latest release (and, during the beta, the latest `1.0.0bN`) receives fixes.

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability or for anything that involves real
conversations, API keys or personal data. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on the project repository.

Include the version (`whatsapp-analyzer --version`), the command, and a **synthetic**
reproduction. Never attach real exports, workspaces (`analyzer.db`) or keys; if a report
needs data, generate it with `whatsapp-analyzer demo-data`.

## What is in scope

- Message text, names, phone numbers or keys reaching logs, error messages, `--json` output,
  exports, the workspace `.gitignore` or any network request without explicit authorization.
- Bypassing the per-invocation authorization (`--authorize-send`), budgets or reconciliation.
- Unsafe handling of crafted input files (path traversal, unbounded resource use).

Out of scope: masking is best-effort pseudonymization, not anonymization (documented in
[docs/llm.md](docs/llm.md)); what a provider does with data you authorize sending to it.

## Design guarantees

No telemetry, no backend, no account. Content leaves the machine only for the provider you
configure, after authorization for that run. Keys live in environment variables, and the
configuration stores only the variable's name.
