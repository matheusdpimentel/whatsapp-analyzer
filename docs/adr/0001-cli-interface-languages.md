# ADR 0001: CLI interface languages

- Status: Accepted
- Date: 2026-11-07

## Context

The CLI is used by people and by coding agents. English is the default, and Brazilian Portuguese
is supported from the first release. Repository artifacts (code, docs, tests, commits) stay in
English regardless of the interface language.

## Decision

- User-facing CLI text lives in TOML catalogs under `src/whatsapp_analyzer/i18n/messages/`,
  one file per language, accessed through `t(key, **params)`.
- Supported languages: `en` (default) and `pt-BR`. Selection order: `--lang`, then `WCA_LANG`,
  then English. The system locale is not used, so behavior does not change between machines
  unexpectedly.
- The language is resolved from the raw arguments before the command tree is built, because
  Typer help texts are fixed at build time.
- All catalogs must define the same keys with the same placeholders. A test enforces this, so
  lookups never fall back to another language at runtime.
- Machine-facing identifiers never change with the language: command names, option names, JSON
  keys, error codes and exit codes stay in English.

## Alternatives considered

- **gettext**: standard, but it requires compiling `.mo` files and extra tooling for a small set
  of strings.
- **Detecting the system locale**: convenient, but makes output differ between developer machines,
  CI and agents.

## Consequences

- Adding a language means adding one TOML file and registering it in `i18n/__init__.py`.
- Strings that come from Click itself (`Usage:`, `Options:`, `--help` text and generic parser
  errors) remain in English.
