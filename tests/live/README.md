# Live provider tests

These tests send ONE synthetic conversation to a real provider and are billed by it. They
never run by default (pytest and CI skip them) and need every one of these, on purpose:

```bash
export ANTHROPIC_API_KEY=...            # or OPENAI_API_KEY: your own key
export WCA_LIVE_PROVIDER=anthropic      # or openai
export WCA_LIVE_AUTHORIZE=anthropic     # must repeat the provider: explicit authorization
# optional: WCA_LIVE_MODEL (default: the cheapest catalog model of that provider)
uv run pytest tests/live -m live -p no:cacheprovider
```

The run uses `--max-cost 0.01`, one attempt and one conversation of synthetic text.
