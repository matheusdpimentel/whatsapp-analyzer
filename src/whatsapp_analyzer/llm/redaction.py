"""Credential redaction for anything that may reach output: error messages, provider codes.

Redaction is a safety net. The primary rule is that request content, headers and credentials
are never put into messages in the first place.
"""

import os
import re
from collections.abc import Mapping

CREDENTIAL_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY")
REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 8

_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),  # OpenAI- and Anthropic-style keys
    re.compile(r"\b(?:(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{8,}|xox[abp]-[A-Za-z0-9-]{8,})"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._\-~+/=]{8,}"),
    # key = value, "key": "value", Authorization: <scheme> <value>
    re.compile(
        r"(?i)\b(api[_-]?key|x-api-key|authorization|access[_-]?token)"
        r"[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic)\s+)?[^\s\"',}]+"
    ),
    # Generic words only when followed by a secret-looking value ("token: expired" stays).
    re.compile(r"(?i)\b(token|secret|password)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]{8,}"),
)


class SecretValue:
    """Holds a credential. Its `repr` and `str` never reveal the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("SecretValue", self._value))


def redact(text: str, environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    for name in CREDENTIAL_ENV_VARS:
        value = environ.get(name, "")
        if len(value) >= _MIN_SECRET_LENGTH:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
