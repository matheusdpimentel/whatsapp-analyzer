import pytest

from whatsapp_analyzer.cli.argv import command_name, split_global_options


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


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["metrics", "run", "-w", "x"], "metrics run"),
        (["metrics", "-w", "dir", "compare", "1", "2"], "metrics compare"),
        (["metrics", "-w", "dir"], "metrics"),
        (["status", "-w", "run"], "status"),
    ],
)
def test_command_name_for_groups(args: list[str], expected: str) -> None:
    assert command_name(args, args[0]) == expected
