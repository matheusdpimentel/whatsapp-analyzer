"""User-facing message catalogs for the CLI.

Catalogs live in `messages/<language>.toml`. All catalogs must define exactly the same keys and
placeholders; `tests/test_i18n.py` enforces this, so a lookup never needs a fallback language.
"""

import tomllib
from collections.abc import Mapping, Sequence
from functools import cache
from importlib import resources
from typing import Any

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "pt-BR")
LANGUAGE_ENV_VAR = "WCA_LANG"
LANGUAGE_OPTION = "--lang"

_CATALOG_FILES = {"en": "en.toml", "pt-BR": "pt_BR.toml"}
_ALIASES = {
    "en": "en",
    "en-us": "en",
    "en_us": "en",
    "pt": "pt-BR",
    "pt-br": "pt-BR",
    "pt_br": "pt-BR",
}

_current_language = DEFAULT_LANGUAGE


def normalize_language(value: str) -> str | None:
    """Return the canonical language tag for `value`, or None if it is not supported."""
    return _ALIASES.get(value.strip().lower())


def detect_language(global_options: Sequence[str], environ: Mapping[str, str]) -> str:
    """Choose the interface language before the CLI parser runs.

    Help texts are rendered while the command tree is built, so the language has to be known
    first. Precedence: `--lang` global option, then the `WCA_LANG` variable, then English.
    Invalid values fall back to English here; the parser rejects them afterwards with a clear
    error.
    """
    requested = _language_option(global_options) or environ.get(LANGUAGE_ENV_VAR)
    if requested:
        return normalize_language(requested) or DEFAULT_LANGUAGE
    return DEFAULT_LANGUAGE


def _language_option(global_options: Sequence[str]) -> str | None:
    for index, arg in enumerate(global_options):
        if arg == LANGUAGE_OPTION and index + 1 < len(global_options):
            return global_options[index + 1]
        if arg.startswith(f"{LANGUAGE_OPTION}="):
            return arg.split("=", 1)[1]
    return None


def set_language(language: str) -> None:
    global _current_language  # noqa: PLW0603 - one process-wide interface language
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported language: {language!r}")
    _current_language = language


def t(key: str, /, **params: object) -> str:
    """Translate `key` into the current language and fill in `params`."""
    template = load_catalog(_current_language)[key]
    return template.format(**params) if params else template


@cache
def load_catalog(language: str) -> dict[str, str]:
    text = (
        resources.files(__package__)
        .joinpath("messages", _CATALOG_FILES[language])
        .read_text(encoding="utf-8")
    )
    return _flatten(tomllib.loads(text))


def _flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in table.items():
        full_key = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, f"{full_key}."))
        elif isinstance(value, str):
            flat[full_key] = value
        else:
            raise TypeError(f"catalog entry {full_key!r} must be a string")
    return flat
