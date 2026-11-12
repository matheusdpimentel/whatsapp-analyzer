"""Explicit authorization before conversation content is sent to a real provider.

Every invocation of `llm run` that may send content needs its own authorization: a new run,
a resumed run, `--retry-failed` and `--resend-unknown` alike. Nothing is implied by an
earlier authorization, an environment variable or a configuration file.

- Interactive (stdin and stderr are terminals, no `--json`): the destination, the model, the
  number of requests, the worst-case tokens and cost and the privacy notice are shown, and
  the person types the destination identity to confirm.
- Non-interactive (agents, scripts): `--authorize-send IDENTITY`, where IDENTITY is exactly the
  configured destination (`anthropic`, `openai`, or `openai-compatible@https://host:443/path`).
  A different value is refused before anything is planned.

Without authorization the command exits with code 12 and `AUTHORIZATION_REQUIRED`, reporting
what would be sent and the exact flag to pass. The run stays planned and nothing was sent.
Authorization never lifts a budget, a rate limit or a reconciliation rule.

Each authorization is recorded in `llm_authorizations` (time, method, destination, model and
notice version), never with content or credentials.
"""

import sqlite3
import sys

import typer

from whatsapp_analyzer.cli.output import CliState, stderr_console
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.runner.service import SendEstimate
from whatsapp_analyzer.store.db import transaction, utc_now

NOTICE_VERSION = 1
FLAG = "--authorize-send"


def check_flag(value: str | None, identity: str) -> None:
    """A given `--authorize-send` must name the configured destination exactly."""
    if value is not None and value != identity:
        raise AppError(
            "AUTHORIZATION_MISMATCH",
            ExitCode.AUTHORIZATION_REQUIRED,
            t("consent.error.mismatch", identity=identity),
            hint=t("consent.hint.flag", flag=f"{FLAG} {identity}"),
            # the given value is not echoed: it could be a secret pasted by mistake
            details={"configured_identity": identity},
        )


def authorize(
    state: CliState,
    *,
    identity: str,
    model: str,
    loopback: bool,
    estimate: SendEstimate,
    flag_value: str | None,
) -> str:
    """Return the method ("flag" or "interactive"), or raise AUTHORIZATION_REQUIRED."""
    if flag_value is not None:
        check_flag(flag_value, identity)
        return "flag"
    if _interactive(state):
        _show(identity, model, loopback, estimate)
        try:
            answer = typer.prompt(
                t("consent.prompt", identity=identity), default="", show_default=False, err=True
            )
        except (typer.Abort, EOFError):
            answer = ""
        if answer.strip() == identity:
            return "interactive"
        raise AppError(
            "AUTHORIZATION_DECLINED",
            ExitCode.AUTHORIZATION_REQUIRED,
            t("consent.error.declined"),
            hint=t("consent.hint.flag", flag=f"{FLAG} {identity}"),
            details=_details(identity, model, loopback, estimate),
        )
    raise AppError(
        "AUTHORIZATION_REQUIRED",
        ExitCode.AUTHORIZATION_REQUIRED,
        t("consent.error.required", identity=identity, requests=estimate.requests),
        hint=t("consent.hint.flag", flag=f"{FLAG} {identity}"),
        details=_details(identity, model, loopback, estimate),
    )


def record(
    connection: sqlite3.Connection, run_id: int, method: str, identity: str, model: str
) -> None:
    with transaction(connection):
        connection.execute(
            "INSERT INTO llm_authorizations (run_id, authorized_at, method, provider, model, "
            "notice_version) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, utc_now(), method, identity, model, NOTICE_VERSION),
        )


def notice_lines(loopback: bool) -> list[str]:
    lines = [t("consent.notice.sent"), t("consent.notice.masking")]
    lines.append(t("consent.notice.loopback" if loopback else "consent.notice.remote"))
    return lines


def _details(
    identity: str, model: str, loopback: bool, estimate: SendEstimate
) -> dict[str, object]:
    return {
        "destination": identity,
        "model": model,
        "loopback": loopback,
        "authorize_with": [FLAG, identity],
        "notice_version": NOTICE_VERSION,
        "notice": notice_lines(loopback),
        "estimate": estimate.to_dict(),
    }


def _interactive(state: CliState) -> bool:
    return not state.json_output and sys.stdin.isatty() and stderr_console().is_terminal


def _show(identity: str, model: str, loopback: bool, estimate: SendEstimate) -> None:
    console = stderr_console()
    console.print(t("consent.title"), style="bold yellow", highlight=False, markup=False)
    console.print(
        t("consent.destination", identity=identity, model=model), highlight=False, markup=False
    )
    cost = (
        t("consent.cost_unknown")
        if estimate.worst_case_cost is None
        else f"{estimate.worst_case_cost} {estimate.currency}"
    )
    console.print(
        t(
            "consent.summary",
            jobs=estimate.jobs,
            requests=estimate.requests,
            max_requests=estimate.max_requests,
            tokens=estimate.worst_case_tokens,
            cost=cost,
        ),
        highlight=False,
        markup=False,
    )
    for line in notice_lines(loopback):
        console.print(f"- {line}", highlight=False, markup=False)
