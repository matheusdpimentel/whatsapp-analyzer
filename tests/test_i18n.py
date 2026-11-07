import string

import pytest

from whatsapp_analyzer.i18n import (
    SUPPORTED_LANGUAGES,
    detect_language,
    load_catalog,
    normalize_language,
    set_language,
    t,
)


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


@pytest.mark.parametrize("language", [lang for lang in SUPPORTED_LANGUAGES if lang != "en"])
def test_catalog_defines_the_same_keys_as_english(language: str) -> None:
    assert set(load_catalog(language)) == set(load_catalog("en"))


@pytest.mark.parametrize("language", [lang for lang in SUPPORTED_LANGUAGES if lang != "en"])
def test_catalog_uses_the_same_placeholders_as_english(language: str) -> None:
    reference = load_catalog("en")
    catalog = load_catalog(language)
    mismatched = {
        key
        for key, template in catalog.items()
        if _placeholders(template) != _placeholders(reference[key])
    }
    assert mismatched == set()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("en", "en"), ("EN-us", "en"), ("pt", "pt-BR"), ("pt_BR", "pt-BR"), (" pt-br ", "pt-BR")],
)
def test_normalize_language_accepts_common_spellings(value: str, expected: str) -> None:
    assert normalize_language(value) == expected


def test_normalize_language_rejects_unknown_values() -> None:
    assert normalize_language("fr") is None


@pytest.mark.parametrize(
    ("global_options", "environ", "expected"),
    [
        ([], {}, "en"),
        ([], {"WCA_LANG": "pt-BR"}, "pt-BR"),
        (["--lang", "pt-BR"], {}, "pt-BR"),
        (["--json", "--lang=pt"], {}, "pt-BR"),
        (["--lang", "en"], {"WCA_LANG": "pt-BR"}, "en"),
        (["--lang", "fr"], {}, "en"),
        (["--lang"], {}, "en"),
    ],
)
def test_detect_language_precedence(
    global_options: list[str], environ: dict[str, str], expected: str
) -> None:
    assert detect_language(global_options, environ) == expected


def test_translation_follows_the_current_language() -> None:
    assert t("doctor.title") == "Environment check"
    set_language("pt-BR")
    assert t("doctor.title") == "Verificação do ambiente"


def test_translation_fills_placeholders() -> None:
    assert t("doctor.summary", ok=1, fail=3) == "1 ok, 3 failures"


def test_unknown_key_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        t("does.not.exist")


def test_set_language_rejects_unsupported_languages() -> None:
    with pytest.raises(ValueError, match="unsupported language"):
        set_language("fr")
    assert t("doctor.title") == "Environment check"
