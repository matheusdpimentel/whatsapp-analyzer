"""Pre-parser inspection of the raw arguments.

Some decisions are needed before Typer parses the command line: the interface language (help
texts are translated when the command tree is built) and whether errors must be reported as JSON.
"""

from collections.abc import Sequence

from whatsapp_analyzer.i18n import LANGUAGE_OPTION

_OPTIONS_WITH_VALUE = frozenset({LANGUAGE_OPTION})


def split_global_options(args: Sequence[str]) -> tuple[list[str], str | None]:
    """Return the global options and the command name.

    Global options are only valid before the command name, matching how the parser treats them;
    anything after the command belongs to that command.
    """
    options: list[str] = []
    expecting_value = False
    for arg in args:
        if expecting_value:
            options.append(arg)
            expecting_value = False
        elif arg == "--":
            break
        elif arg.startswith("-"):
            options.append(arg)
            expecting_value = arg in _OPTIONS_WITH_VALUE
        else:
            return options, arg
    return options, None


COMMAND_GROUPS = {
    "metrics": frozenset({"run", "compare"}),
    "llm": frozenset({"prompts", "preview", "run", "status", "export"}),
}


def command_name(args: Sequence[str], command: str | None) -> str | None:
    """`command`, or `group subcommand` for grouped commands (e.g. "metrics run")."""
    if command not in COMMAND_GROUPS:
        return command
    following = list(args[list(args).index(command) + 1 :])
    subcommand = next((arg for arg in following if arg in COMMAND_GROUPS[command]), None)
    return f"{command} {subcommand}" if subcommand else command
