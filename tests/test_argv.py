import pytest

from whatsapp_analyzer.cli.argv import split_global_options


@pytest.mark.parametrize(
    ("args", "options", "command"),
    [
        ([], [], None),
        (["doctor"], [], "doctor"),
        (["--json", "doctor"], ["--json"], "doctor"),
        (["--lang", "pt-BR", "doctor"], ["--lang", "pt-BR"], "doctor"),
        (["--lang=pt", "--json", "doctor"], ["--lang=pt", "--json"], "doctor"),
        (["doctor", "--lang", "pt-BR"], [], "doctor"),
        (["--json", "--", "doctor"], ["--json"], None),
        (["--lang"], ["--lang"], None),
    ],
)
def test_split_global_options(args: list[str], options: list[str], command: str | None) -> None:
    assert split_global_options(args) == (options, command)
