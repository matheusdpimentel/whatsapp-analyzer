"""The minimal adapter of docs/adding-a-provider.md, kept here so the guide stays true.

It talks to no service: `complete` answers locally with a valid classification. A real
adapter replaces the body of `complete` with one SDK or HTTP call and maps its errors with
`llm.providers._http`, exactly like `openai_chat.py` and `anthropic_messages.py`.
"""

from whatsapp_analyzer.llm.providers import ProviderSpec
from whatsapp_analyzer.llm.providers.base import AdapterConfig, usage_from
from whatsapp_analyzer.llm.tokens import estimate_text_tokens
from whatsapp_analyzer.llm.types import FinishReason, LLMRequest, LLMResult

ANSWER = '{"level": "LOW", "reasons": ["SHORT_ACKNOWLEDGEMENTS_ONLY"], "evidence": []}'


class EchoProvider:
    def __init__(self, config: AdapterConfig) -> None:
        self._config = config  # identity, model, api key, endpoint, options, output mode

    @property
    def name(self) -> str:
        return self._config.identity  # stored with every attempt and used for reuse

    async def complete(self, request: LLMRequest) -> LLMResult:
        prompt = request.system + "".join(m.content for m in request.messages)
        return LLMResult(
            text=ANSWER,
            finish_reason=FinishReason.STOP,
            usage=usage_from(
                input_uncached=estimate_text_tokens(prompt),
                cache_read=0,
                cache_write=0,
                output_total=estimate_text_tokens(ANSWER),
                output_reasoning=None,  # not reported: unknown, never 0
                cache_write_billed=False,
            ),
            model_reported=request.model,
            provider_request_id=None,
        )


def check_options(options: dict[str, object]) -> str | None:
    return None  # no options: every name is already rejected by `options=frozenset()`


SPEC = ProviderSpec(
    name="example",
    package=None,  # an SDK-based adapter names its package, e.g. "openai"
    log_env=None,
    default_key_env="EXAMPLE_API_KEY",
    base_url_required=False,
    options=frozenset(),
    check_options=check_options,
    temperature_possible=False,
    official=True,
    factory=EchoProvider,
)
