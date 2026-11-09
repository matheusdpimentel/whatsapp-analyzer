"""Provider-independent LLM core: contracts, prompts, context preparation, output validation,
token accounting and cost estimates.

Milestone 4 is offline: the only provider is `FakeProvider`. Real adapters, the runner and
persistence of calls come in later milestones. The semantic classification produced here is
kept separate from the deterministic metrics of `whatsapp_analyzer.metrics`.
"""
