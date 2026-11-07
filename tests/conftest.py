from collections.abc import Iterator

import pytest

from whatsapp_analyzer.i18n import DEFAULT_LANGUAGE, LANGUAGE_ENV_VAR, set_language


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep tests independent of the developer's shell and of each other."""
    for name in (LANGUAGE_ENV_VAR, "WCA_DEBUG", "COLUMNS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COLUMNS", "160")  # stable table wrapping in human output
    yield
    set_language(DEFAULT_LANGUAGE)
