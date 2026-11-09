"""Local token ESTIMATES. They are not exact counts and are always labeled as estimates.

Tokenizers differ between providers and models, and exact counting either needs a
provider-specific library or an API call that sends the text. The estimate is deliberately
conservative (about 3 characters per token, plus a fixed overhead per message), which is
higher than typical counts for English and Portuguese text. It is used for the context budget
and cost previews only; provider-reported usage replaces it in later milestones.
"""

import math

CHARS_PER_TOKEN = 3
MESSAGE_OVERHEAD_TOKENS = 4
REQUEST_OVERHEAD_TOKENS = 8


def estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_prompt_tokens(system: str, contents: list[str]) -> int:
    return (
        REQUEST_OVERHEAD_TOKENS
        + estimate_text_tokens(system)
        + sum(estimate_text_tokens(c) + MESSAGE_OVERHEAD_TOKENS for c in contents)
    )
