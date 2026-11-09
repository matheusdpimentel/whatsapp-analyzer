"""Persistent LLM runner: plans a run over frozen conversation versions, executes it with
bounded concurrency, rate limits and budgets, checkpoints every attempt before the provider
call, and resumes without losing confirmed results. See docs/runner.md and ADR 0006.

Milestone 5 runs only the offline `FakeProvider`. The runner tables never hold message text,
prompts or raw provider responses.
"""
