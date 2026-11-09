"""The provider contract.

The interface is asynchronous (ADR 0005): the milestone 5 runner can then cancel calls,
enforce timeouts with `asyncio.timeout` and bound concurrency with semaphores without
refactoring. A synchronous SDK can be adapted with `asyncio.to_thread`; the reverse (cancelling
a blocking call already in flight) has no clean solution. The contract knows nothing about the
CLI, the database or the runner.
"""

from typing import Protocol, runtime_checkable

from whatsapp_analyzer.llm.types import LLMRequest, LLMResult


@runtime_checkable
class Provider(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResult:
        """Run one request. Raises `ProviderError` for failures; must not log or include
        request content, headers or credentials in errors. Must never suppress
        `asyncio.CancelledError`: timeouts and cancellation rely on it."""
        ...
