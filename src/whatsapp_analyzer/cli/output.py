"""Human and JSON output for CLI commands.

JSON mode writes exactly one JSON document per invocation to stdout, so agents can parse it
without scraping text. The envelope is versioned by `schema`.
"""

import json
import sys
from dataclasses import dataclass

from rich.console import Console

from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.i18n import t

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CliState:
    json_output: bool


def stdout_console() -> Console:
    return Console()


def stderr_console() -> Console:
    return Console(stderr=True)


def write_json(payload: dict[str, object]) -> None:
    # ASCII-only output keeps the stream valid on consoles with legacy encodings (e.g. cp1252).
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def result_payload(
    command: str,
    data: dict[str, object],
    error: AppError | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "ok": error is None,
        "command": command,
        "data": data,
    }
    if error is not None:
        payload["error"] = error_body(error)
    return payload


def error_payload(command: str | None, error: AppError) -> dict[str, object]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "error": error_body(error),
    }


def error_body(error: AppError) -> dict[str, object]:
    body: dict[str, object] = {
        "code": error.code,
        "message": error.message,
        "exit_code": int(error.exit_code),
    }
    if error.hint is not None:
        body["hint"] = error.hint
    if error.details is not None:
        body["details"] = error.details
    return body


def print_error(error: AppError) -> None:
    console = stderr_console()
    console.print(f"[bold red]{t('cli.error.label')}[/] [dim]({error.code})[/]: ", end="")
    console.print(error.message, markup=False, highlight=False)
    if error.hint is not None:
        console.print(error.hint, markup=False, highlight=False, style="dim")
