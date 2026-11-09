"""`llm prompts` and `llm preview`: inspect templates and the prepared request, fully offline.

`llm preview` shows structure only by default (counts, estimates, hashes, flags). The masked
prompt is shown or written only with `--show-content` / `--out`, and only after an explicit
confirmation for that content: an interactive "yes", or `--confirm-content` when there is no
terminal (agents, scripts). The flag confirms content output and nothing else; the location and
overwrite checks for `--out` still apply. Content is never logged.
"""

import asyncio
import contextlib
import functools
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from whatsapp_analyzer.cli.output import (
    CliState,
    result_payload,
    stderr_console,
    stdout_console,
    write_json,
)
from whatsapp_analyzer.cli.workspace_support import (
    WorkspaceOption,
    config_error,
    load_settings,
    open_database,
    resolve_workspace,
)
from whatsapp_analyzer.config import ConfigError, Settings
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.llm.call import build_request, classify
from whatsapp_analyzer.llm.context import ContextError, PreparedContext, build_context
from whatsapp_analyzer.llm.fake import FakeProvider
from whatsapp_analyzer.llm.masking import name_from_key
from whatsapp_analyzer.llm.pricing import Price, estimate_cost, load_prices
from whatsapp_analyzer.llm.prompts import (
    InvalidTemplate,
    PromptError,
    PromptTemplate,
    builtin_templates,
    find_template,
    workspace_templates,
)
from whatsapp_analyzer.llm.tokens import estimate_prompt_tokens
from whatsapp_analyzer.llm.types import LLMRequest, Usage
from whatsapp_analyzer.metrics.raw import StoredMessage
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType
from whatsapp_analyzer.workspace import Workspace

CONFIRMATION_WORD = "yes"
PRICING_FILE = "pricing.toml"
PROMPTS_DIR = "prompts"
GIT_TIMEOUT_SECONDS = 10


def prompts(ctx: typer.Context, workspace: WorkspaceOption = None) -> None:
    state: CliState = ctx.obj
    available = builtin_templates()
    invalid: list[InvalidTemplate] = []
    if workspace is not None:
        found, invalid = workspace_templates(resolve_workspace(workspace).root / PROMPTS_DIR)
        available += found
    data: dict[str, object] = {
        "templates": [template.summary() for template in available],
        "invalid": [{"file": item.file, "error": item.error} for item in invalid],
    }
    if state.json_output:
        write_json(result_payload("llm prompts", data))
        return
    table = Table(title=t("llm.prompts.title"), title_justify="left")
    for name in ("id", "version", "origin", "hash"):
        table.add_column(t(f"llm.prompts.column.{name}"))
    for template in available:
        table.add_row(
            *(escape(v) for v in (template.id, template.version, template.origin)),
            template.template_hash[:12],
        )
    stdout_console().print(table)
    for item in invalid:
        stderr_console().print(
            t("llm.prompts.invalid", file=item.file, reason=t(f"llm.prompt_error.{item.error}")),
            style="yellow",
            markup=False,
            highlight=False,
        )


def preview(
    ctx: typer.Context,
    conversation: Annotated[
        str, typer.Argument(metavar="CONVERSATION", help=t("llm.preview.arg.conversation"))
    ],
    workspace: WorkspaceOption = None,
    source_format: Annotated[
        str | None, typer.Option(metavar="FORMAT", help=t("llm.preview.option.source_format"))
    ] = None,
    prompt: Annotated[
        str | None, typer.Option(metavar="ID|FILE", help=t("llm.preview.option.prompt"))
    ] = None,
    config: Annotated[
        Path | None, typer.Option(metavar="FILE", help=t("llm.preview.option.config"))
    ] = None,
    simulate: Annotated[bool, typer.Option(help=t("llm.preview.option.simulate"))] = False,
    show_content: Annotated[bool, typer.Option(help=t("llm.preview.option.show_content"))] = False,
    out: Annotated[
        Path | None, typer.Option(metavar="FILE", help=t("llm.preview.option.out"))
    ] = None,
    overwrite: Annotated[bool, typer.Option(help=t("llm.preview.option.overwrite"))] = False,
    confirm_content: Annotated[
        bool, typer.Option(help=t("llm.preview.option.confirm_content"))
    ] = False,
    allow_git_worktree: Annotated[
        bool, typer.Option(help=t("llm.preview.option.allow_git_worktree"))
    ] = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    settings = load_settings(target, config)
    out_target = check_output_path(out, overwrite, allow_git_worktree) if out is not None else None
    prepared = _prepare(target, settings, conversation, source_format, prompt)

    data = _summary(prepared, settings, target)
    if simulate:
        outcome = asyncio.run(
            classify(FakeProvider(), prepared.request, prepared.context, prepared.price)
        )
        data["simulation"] = outcome.telemetry.to_dict()

    content = None
    if show_content or out is not None:
        _confirm_content(state, confirm_content)
        content = _content(prepared)
        if out is not None and out_target is not None:
            # Checked again after the prompt; the write is bound to the same directory.
            rechecked = check_output_path(out, overwrite, allow_git_worktree)
            if (rechecked.device, rechecked.inode) != (out_target.device, out_target.inode):
                raise _location_changed(out_target)
            write_private_file(rechecked, content, overwrite)
            data["written_to"] = str(out)
        if show_content:
            data["content"] = content

    if state.json_output:
        write_json(result_payload("llm preview", data))
        return
    _render(data, content if show_content else None)


@dataclass(frozen=True)
class _PreparedRun:
    template: PromptTemplate
    context: PreparedContext
    request: LLMRequest
    conversation_id: int
    price: Price | None


def _prepare(
    workspace: Workspace,
    settings: Settings,
    key: str,
    source_format: str | None,
    prompt: str | None,
) -> _PreparedRun:
    try:
        # A --prompt path is relative to the current directory; one from analyzer.toml is
        # relative to the workspace, wherever the command runs.
        template = find_template(
            prompt or settings.llm.prompt,
            workspace.root / PROMPTS_DIR,
            base_dir=None if prompt else workspace.root,
        )
    except PromptError as exc:
        raise prompt_error(exc) from exc
    connection = open_database(workspace)
    try:
        conversation_id, is_group, messages = _load_conversation(connection, key, source_format)
    finally:
        connection.close()
    try:
        context = build_context(
            messages,
            settings.llm.context,
            is_group=is_group,
            ignored_texts=settings.metrics.ignored_texts,
            contact_names=[name_from_key(key)],  # often the contact's name (chat title, file)
        )
    except ContextError as exc:
        raise AppError(exc.kind, ExitCode.USAGE_ERROR, t(f"llm.preview.error.{exc.kind}")) from exc
    try:
        _, request = build_request(template, context, settings.llm.model)
    except PromptError as exc:
        raise prompt_error(exc) from exc
    try:
        prices = load_prices(workspace.root / PRICING_FILE)
    except ConfigError as exc:
        raise config_error(exc) from exc
    # Prices in effect today; the clock never affects classifications.
    price = prices.lookup(settings.llm.provider, settings.llm.model, date.today())
    return _PreparedRun(template, context, request, conversation_id, price)


def _load_conversation(
    connection: sqlite3.Connection, key: str, source_format: str | None
) -> tuple[int, bool, list[StoredMessage]]:
    query = (
        "SELECT c.id, c.current_version_id, v.is_group FROM conversations c "
        "JOIN conversation_versions v ON v.id = c.current_version_id WHERE c.conversation_key = ?"
    )
    params: tuple[str, ...] = (key,)
    if source_format is not None:
        query += " AND c.source_format = ?"
        params = (key, source_format)
    rows = connection.execute(query, params).fetchall()
    if not rows:
        raise AppError(
            "CONVERSATION_NOT_FOUND",
            ExitCode.USAGE_ERROR,
            t("llm.preview.error.CONVERSATION_NOT_FOUND"),
        )
    if len(rows) > 1:
        raise AppError(
            "AMBIGUOUS_CONVERSATION",
            ExitCode.USAGE_ERROR,
            t("llm.preview.error.AMBIGUOUS_CONVERSATION", count=len(rows)),
            hint=t("llm.preview.hint.source_format"),
        )
    conversation_id, version_id, is_group = rows[0]
    messages = [
        StoredMessage(
            seq=row[0],
            timestamp=datetime.fromisoformat(row[1]),
            sender=row[2],
            direction=Direction(row[3]) if row[3] else None,
            text=row[4],
            message_type=MessageType(row[5]),
            media_kind=MediaKind(row[6]) if row[6] else None,
        )
        for row in connection.execute(
            "SELECT seq, timestamp, sender, direction, text, message_type, media_kind "
            "FROM messages WHERE version_id = ? ORDER BY timestamp, seq",
            (version_id,),
        )
    ]
    return conversation_id, bool(is_group), messages


def _summary(prepared: _PreparedRun, settings: Settings, workspace: Workspace) -> dict[str, object]:
    request = prepared.request
    input_tokens = estimate_prompt_tokens(request.system, [m.content for m in request.messages])
    upper_bound = Usage(
        input_uncached=input_tokens,
        input_cache_read=0,
        input_cache_write=0,
        output_total=request.max_output_tokens,
        estimated=True,
    )
    return {
        "conversation_id": prepared.conversation_id,
        "provider": settings.llm.provider,
        "model": settings.llm.model,
        "template": prepared.template.summary(),
        "request_id": request.request_id,
        "prompt_hash": request.prompt_hash,
        "context": prepared.context.summary(settings.llm.context),
        "tokens": {
            "input_estimated": input_tokens,
            "max_output": request.max_output_tokens,
            "estimated": True,
        },
        "cost_upper_bound": estimate_cost(upper_bound, prepared.price).to_dict(),
        "pricing_file": str(workspace.root / PRICING_FILE),
    }


def _content(prepared: _PreparedRun) -> str:
    request = prepared.request
    parts = ["## system", request.system.rstrip(), ""]
    for message in request.messages:
        parts += [f"## {message.role.value}", message.content.rstrip(), ""]
    return "\n".join(parts)


def _confirm_content(state: CliState, confirmed: bool) -> None:
    if confirmed:
        return
    interactive = sys.stdin.isatty() and sys.stderr.isatty() and not state.json_output
    if not interactive:
        raise AppError(
            "CONTENT_CONFIRMATION_REQUIRED",
            ExitCode.USAGE_ERROR,
            t("llm.preview.error.confirmation_required"),
            hint=t("llm.preview.hint.confirm_content"),
        )
    stderr_console().print(t("llm.preview.warning.content"), style="yellow", highlight=False)
    answer = typer.prompt(t("llm.preview.prompt.confirm", word=CONFIRMATION_WORD), default="")
    if answer.strip().lower() != CONFIRMATION_WORD:
        raise AppError(
            "CONTENT_NOT_CONFIRMED", ExitCode.USAGE_ERROR, t("llm.preview.error.not_confirmed")
        )


@dataclass(frozen=True)
class OutputTarget:
    """An output location that passed the checks: the real directory, its identity (device and
    inode) at check time, and the file name inside it."""

    directory: Path
    name: str
    device: int
    inode: int


def check_output_path(path: Path, overwrite: bool, allow_git_worktree: bool) -> OutputTarget:
    """Refuse symlinks, silent overwrites and locations Git could commit."""
    if path.name in {"", ".", ".."}:
        raise _output_refused("OUTPUT_IS_DIRECTORY", path)
    if path.is_symlink():
        raise _output_refused("OUTPUT_IS_SYMLINK", path)
    if path.exists():
        if path.is_dir():
            raise _output_refused("OUTPUT_IS_DIRECTORY", path)
        if not overwrite:
            raise AppError(
                "OUTPUT_EXISTS",
                ExitCode.USAGE_ERROR,
                t("llm.preview.error.OUTPUT_EXISTS", path=path),
                hint=t("llm.preview.hint.overwrite"),
            )
    try:
        directory = path.parent.resolve(strict=True)
    except OSError as exc:
        raise _output_refused("OUTPUT_PARENT_MISSING", path) from exc
    if not directory.is_dir():
        raise _output_refused("OUTPUT_PARENT_MISSING", path)
    if not allow_git_worktree and not git_would_ignore(directory, path.name):
        raise AppError(
            "OUTPUT_IN_GIT_WORKTREE",
            ExitCode.USAGE_ERROR,
            t("llm.preview.error.OUTPUT_IN_GIT_WORKTREE", path=path),
            hint=t("llm.preview.hint.git_worktree"),
        )
    info = directory.stat()
    return OutputTarget(directory, path.name, info.st_dev, info.st_ino)


def git_would_ignore(directory: Path, name: str) -> bool:
    """True when Git could not commit `directory/name`: the directory is outside any work tree,
    or Git itself reports the path as ignored. Git's own rules decide, so negations (`!x`),
    nested and global ignore files and info/exclude all count, and a tracked file is never
    considered ignored. Without a usable `git`, any `.git` above the directory is unsafe."""
    git = shutil.which("git")
    if git is None:
        return not _has_git_marker(directory)
    try:
        inside = _git(git, directory, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return inside.returncode != 0 and not _has_git_marker(directory)
        ignored = _git(git, directory, "check-ignore", "-q", "--", name)
    except (OSError, subprocess.SubprocessError):
        return not _has_git_marker(directory)
    return ignored.returncode == 0


def _git(git: str, directory: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # fixed arguments, no shell
        [git, "-C", str(directory), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
        check=False,
    )


def _has_git_marker(directory: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (directory, *directory.parents))


def write_private_file(target: OutputTarget, content: str, overwrite: bool) -> None:
    """Write with mode 0600 into the directory that was checked.

    On POSIX the directory is opened once (without following a symlink) and its identity is
    compared with the one recorded by `check_output_path`; the file is then created relative to
    that open directory, so swapping a path component after the check cannot redirect the
    write. Without `overwrite` the file must not exist (O_EXCL); with it, a temporary file in
    the same directory replaces the old one atomically. Windows has no `dir_fd` support: the
    identity is re-checked just before writing by path.
    """
    data = content.encode("utf-8")
    try:
        if dir_fd_writes_supported():
            _write_in_directory(target, data, overwrite)
        else:
            _check_identity(target, target.directory.stat())
            _write_by_path(target.directory / target.name, data, overwrite)
    except FileExistsError as exc:
        raise AppError(
            "OUTPUT_EXISTS",
            ExitCode.USAGE_ERROR,
            t("llm.preview.error.OUTPUT_EXISTS", path=target.directory / target.name),
        ) from exc
    except OSError as exc:
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t(
                "export.error.not_writable",
                path=target.directory / target.name,
                reason=exc.strerror or type(exc).__name__,
            ),
        ) from exc


@functools.cache
def dir_fd_writes_supported() -> bool:
    """Whether the exact operations used by the protected write work here: opening a
    directory, creating a file relative to it, replacing and unlinking relative to it.

    `os.supports_dir_fd` is not reliable for this: on macOS it omits `os.replace` although
    `os.replace(..., src_dir_fd=..., dst_dir_fd=...)` works. So the operations are tried once
    in a private temporary directory.
    """
    if os.name == "nt":
        return False
    try:
        with tempfile.TemporaryDirectory() as probe:
            directory = os.open(probe, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                handle = os.open("a", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                os.close(handle)
                os.replace("a", "b", src_dir_fd=directory, dst_dir_fd=directory)
                os.unlink("b", dir_fd=directory)
            finally:
                os.close(directory)
    except (OSError, NotImplementedError, TypeError):
        return False
    return True


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _write_in_directory(target: OutputTarget, data: bytes, overwrite: bool) -> None:
    try:
        directory_fd = os.open(
            target.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
        )
    except OSError as exc:  # gone, or replaced by a symlink (ELOOP)
        raise _location_changed(target) from exc
    try:
        _check_identity(target, os.fstat(directory_fd))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        name = target.name if not overwrite else f".{target.name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(name, target.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except BaseException:
            if overwrite:
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=directory_fd)
            raise
    finally:
        os.close(directory_fd)


def _write_by_path(path: Path, data: bytes, overwrite: bool) -> None:
    if not overwrite:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        return
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _check_identity(target: OutputTarget, info: os.stat_result) -> None:
    if (info.st_dev, info.st_ino) != (target.device, target.inode):
        raise _location_changed(target)


def _location_changed(target: OutputTarget) -> AppError:
    return AppError(
        "OUTPUT_LOCATION_CHANGED",
        ExitCode.OUTPUT_ERROR,
        t("llm.preview.error.OUTPUT_LOCATION_CHANGED", path=target.directory / target.name),
    )


def _output_refused(code: str, path: Path) -> AppError:
    return AppError(code, ExitCode.USAGE_ERROR, t(f"llm.preview.error.{code}", path=path))


def prompt_error(exc: PromptError) -> AppError:
    return AppError(
        "PROMPT_INVALID" if exc.kind != "unknown_template" else "PROMPT_NOT_FOUND",
        ExitCode.CONFIG_ERROR,
        t(f"llm.prompt_error.{exc.kind}", **exc.details),
        hint=t("llm.prompt_error.hint"),
    )


def _render(data: dict[str, object], content: str | None) -> None:
    console = stdout_console()
    template = data["template"]
    context = data["context"]
    tokens = data["tokens"]
    cost = data["cost_upper_bound"]
    assert isinstance(template, dict) and isinstance(context, dict)
    assert isinstance(tokens, dict) and isinstance(cost, dict)
    table = Table(title=t("llm.preview.title"), title_justify="left", show_header=False)
    table.add_column()
    table.add_column()
    masking, flags = context["masking"], context["security_flags"]
    assert isinstance(masking, dict) and isinstance(flags, list)
    rows = (
        ("conversation", f"#{data['conversation_id']}"),
        ("provider", f"{data['provider']} / {data['model']}"),
        ("template", escape(f"{template['id']}@{template['version']} ({template['origin']})")),
        ("messages_shown", str(context["messages_shown"])),
        ("messages_omitted", str(context["messages_omitted"])),
        ("messages_truncated", str(context["messages_truncated"])),
        (
            "masking",
            ", ".join(f"{k} {v}" for k, v in sorted(masking.items())) or "-",
        ),
        ("input_tokens", t("llm.preview.estimate", value=tokens["input_estimated"])),
        ("max_output", str(tokens["max_output"])),
        ("cost", _cost_text(cost)),
        ("security_flags", ", ".join(map(str, flags)) or "-"),
        ("request_id", str(data["request_id"])[:12]),
    )
    for key, value in rows:
        table.add_row(t(f"llm.preview.row.{key}"), value)
    console.print(table)
    if flags:
        stderr_console().print(t("llm.preview.warning.flags"), style="yellow", highlight=False)
    simulation = data.get("simulation")
    if isinstance(simulation, dict):
        console.print(
            t(
                "llm.preview.simulation",
                status=simulation["status"],
                label=simulation["label"] or "-",
            ),
            highlight=False,
        )
    if "written_to" in data:
        key = "llm.preview.written_windows" if os.name == "nt" else "llm.preview.written"
        console.print(t(key, path=data["written_to"]), markup=False, highlight=False)
    stderr_console().print(t("llm.preview.masking_notice"), style="dim", highlight=False)
    if content is not None:
        # Written as is: no markup, emoji codes or wrapping, so it matches what would be sent.
        sys.stdout.write(content)
        sys.stdout.flush()


def _cost_text(cost: dict[str, object]) -> str:
    if cost["total"] is None:
        missing = cost["missing"]
        assert isinstance(missing, list)
        return t("llm.preview.cost_unknown", missing=", ".join(map(str, missing)))
    return t("llm.preview.cost_estimate", total=cost["total"], currency=cost["currency"])
