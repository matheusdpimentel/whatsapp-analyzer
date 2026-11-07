import io
import os
import sys
from collections.abc import Sequence
from typing import Annotated

import typer

from whatsapp_analyzer import __version__
from whatsapp_analyzer.cli.argv import split_global_options
from whatsapp_analyzer.cli.commands.doctor import doctor
from whatsapp_analyzer.cli.output import CliState, error_payload, print_error, write_json
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import (
    LANGUAGE_ENV_VAR,
    LANGUAGE_OPTION,
    SUPPORTED_LANGUAGES,
    detect_language,
    normalize_language,
    set_language,
    t,
)

PROG_NAME = "whatsapp-analyzer"
JSON_OPTION = "--json"
DEBUG_ENV_VAR = "WCA_DEBUG"


def build_app() -> typer.Typer:
    """Build the command tree. Call after the interface language is set: help texts are
    translated at build time."""
    app = typer.Typer(
        name=PROG_NAME,
        help=t("cli.help"),
        add_completion=False,
        pretty_exceptions_enable=False,
    )

    @app.callback(invoke_without_command=True)
    def root(
        ctx: typer.Context,
        json_output: Annotated[bool, typer.Option(JSON_OPTION, help=t("cli.option.json"))] = False,
        lang: Annotated[
            str | None,
            typer.Option(
                LANGUAGE_OPTION,
                envvar=LANGUAGE_ENV_VAR,
                metavar="LANG",
                help=t("cli.option.lang"),
                callback=_validate_language,
            ),
        ] = None,
        version: Annotated[
            bool,
            typer.Option(
                "--version", help=t("cli.option.version"), callback=_show_version, is_eager=True
            ),
        ] = False,
    ) -> None:
        ctx.obj = CliState(json_output=json_output)
        if ctx.invoked_subcommand is None:
            if json_output:
                raise AppError(
                    "USAGE_ERROR",
                    ExitCode.USAGE_ERROR,
                    t("cli.error.missing_command"),
                    hint=t("cli.error.usage_hint", prog=PROG_NAME),
                )
            sys.stdout.write(ctx.get_help() + "\n")
            raise typer.Exit(ExitCode.OK)

    app.command("doctor", help=t("doctor.help"))(doctor)
    return app


def _validate_language(value: str | None) -> str | None:
    # The language is already active (see `main`); this only rejects unsupported values.
    if value is not None and normalize_language(value) is None:
        raise typer.BadParameter(
            t(
                "cli.error.unsupported_language",
                value=value,
                supported=", ".join(SUPPORTED_LANGUAGES),
            )
        )
    return value


def _show_version(value: bool) -> None:
    if value:
        sys.stdout.write(f"{PROG_NAME} {__version__}\n")
        raise typer.Exit(ExitCode.OK)


def main(argv: Sequence[str] | None = None) -> int:
    _never_fail_on_output_encoding()
    args = list(sys.argv[1:] if argv is None else argv)
    global_options, command = split_global_options(args)
    set_language(detect_language(global_options, os.environ))
    json_mode = JSON_OPTION in global_options

    try:
        result = build_app()(args=args, prog_name=PROG_NAME, standalone_mode=False)
    except typer.TyperException as exc:  # parser errors: unknown command, bad option value
        error = AppError(
            "USAGE_ERROR",
            ExitCode.USAGE_ERROR,
            exc.format_message(),
            hint=t("cli.error.usage_hint", prog=PROG_NAME),
        )
        return _report(json_mode, command, error)
    except (typer.Abort, KeyboardInterrupt):
        return _report(
            json_mode,
            command,
            AppError("INTERRUPTED", ExitCode.INTERRUPTED, t("cli.error.interrupted")),
        )
    except AppError as exc:
        return _report(json_mode, command, exc)
    except Exception as exc:
        if os.environ.get(DEBUG_ENV_VAR) == "1":
            raise
        # Only the exception type is shown: messages may contain conversation content.
        error = AppError(
            "INTERNAL_ERROR",
            ExitCode.INTERNAL_ERROR,
            t("cli.error.internal", error_type=type(exc).__name__),
        )
        return _report(json_mode, command, error)
    return int(result) if isinstance(result, int) else ExitCode.OK


def _report(json_mode: bool, command: str | None, error: AppError) -> int:
    if json_mode:
        write_json(error_payload(command, error))
    else:
        print_error(error)
    return error.exit_code


def _never_fail_on_output_encoding() -> None:
    # Translated text may not be encodable on a legacy console (e.g. ASCII); replace characters
    # instead of crashing in the middle of a report.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
