"""`llm review` and `llm consolidate` (milestone 7).

`llm review` runs the reviewer configured in `[llm.review]` over versions selected from a
finished primary run, as an ordinary run: same checkpoints, budgets, reuse, reconciliation,
exit codes and per-invocation authorization (`--authorize-send` names the REVIEWER's
destination). `llm consolidate` compares the two runs per conversation with a policy and
writes a CSV with its metadata; it never changes either run.
"""

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from whatsapp_analyzer.cli.commands.llm_run import (
    RunArgs,
    _overrides,
    prepare_run,
    run_configured,
)
from whatsapp_analyzer.cli.output import CliState, result_payload, stdout_console, write_json
from whatsapp_analyzer.cli.workspace_support import (
    WorkspaceOption,
    load_settings,
    open_database,
    resolve_workspace,
)
from whatsapp_analyzer.config import ConsolidationPolicy, ReviewMode, Settings
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.runner import review as reviews
from whatsapp_analyzer.runner.service import OpenedRun
from whatsapp_analyzer.workspace import Workspace


def review(
    ctx: typer.Context,
    workspace: WorkspaceOption = None,
    config: Annotated[
        Path | None, typer.Option(metavar="FILE", help=t("llm.run.option.config"))
    ] = None,
    primary_run: Annotated[
        int | None, typer.Option(metavar="ID", help=t("llm.review.option.primary_run"))
    ] = None,
    conversation: Annotated[
        list[str] | None,
        typer.Option(metavar="KEY", help=t("llm.review.option.conversation")),
    ] = None,
    reuse: Annotated[bool, typer.Option(help=t("llm.run.option.reuse"))] = True,
    retry_failed: Annotated[bool, typer.Option(help=t("llm.run.option.retry_failed"))] = False,
    resend_unknown: Annotated[bool, typer.Option(help=t("llm.run.option.resend_unknown"))] = False,
    concurrency: Annotated[
        int | None, typer.Option(min=1, max=64, help=t("llm.run.option.concurrency"))
    ] = None,
    rpm: Annotated[int | None, typer.Option(min=1, help=t("llm.run.option.rpm"))] = None,
    tpm: Annotated[int | None, typer.Option(min=1, help=t("llm.run.option.tpm"))] = None,
    max_tokens: Annotated[
        int | None, typer.Option(min=1, help=t("llm.run.option.max_tokens"))
    ] = None,
    max_cost: Annotated[
        str | None, typer.Option(metavar="AMOUNT", help=t("llm.run.option.max_cost"))
    ] = None,
    max_attempts: Annotated[
        int | None, typer.Option(min=1, max=10, help=t("llm.run.option.max_attempts"))
    ] = None,
    fake_scenario: Annotated[
        str | None, typer.Option(metavar="SCENARIO", help=t("llm.run.option.fake_scenario"))
    ] = None,
    events: Annotated[
        str | None, typer.Option(metavar="FILE|-", help=t("llm.run.option.events"))
    ] = None,
    authorize_send: Annotated[
        str | None,
        typer.Option(
            "--authorize-send", metavar="DESTINATION", help=t("llm.review.option.authorize_send")
        ),
    ] = None,
    reselect: Annotated[bool, typer.Option(help=t("llm.review.option.reselect"))] = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    settings = load_settings(target, config)
    review_settings = settings.llm.review
    if review_settings.mode is ReviewMode.SINGLE or review_settings.reviewer is None:
        raise AppError(
            "REVIEW_DISABLED",
            ExitCode.CONFIG_ERROR,
            t("llm.review.error.disabled"),
            hint=t("llm.review.hint.disabled"),
        )
    if conversation and review_settings.mode is ReviewMode.DUAL:
        raise AppError(
            "INCOMPATIBLE_OPTIONS",
            ExitCode.USAGE_ERROR,
            t("llm.review.error.conversation_dual"),
        )
    reviewer_settings = replace(settings, llm=review_settings.reviewer)
    args = RunArgs(
        reuse=reuse,
        retry_failed=retry_failed,
        resend_unknown=resend_unknown,
        overrides=_overrides(concurrency, rpm, tpm, max_tokens, max_cost, max_attempts),
        fake_scenario=fake_scenario,
        events=events,
        authorize_send=authorize_send,
    )
    prepared = prepare_run(target, reviewer_settings, args)
    reviewer_config = prepared[0]
    primary, selection = _select(
        target, settings, primary_run, conversation, reviewer_config.run_key(), reselect
    )
    # An independent analysis needs another model: the same provider and model (whatever the
    # generation settings) could reuse the primary results or repeat the same judgment.
    if (reviewer_config.provider, reviewer_config.model) == (primary["provider"], primary["model"]):
        raise AppError(
            "REVIEWER_SAME_AS_PRIMARY",
            ExitCode.CONFIG_ERROR,
            t("llm.review.error.same_as_primary"),
            hint=t("llm.review.hint.same_as_primary"),
            details={"provider": primary["provider"], "model": primary["model"]},
        )
    # Both models must see exactly the conversation the primary run prepared.
    if reviewer_config.context_config_hash() != primary["context_config_hash"]:
        raise AppError(
            "REVIEW_CONTEXT_MISMATCH",
            ExitCode.CONFIG_ERROR,
            t("llm.review.error.context_mismatch", run_id=primary["id"]),
            hint=t("llm.review.hint.context_mismatch"),
            details={"primary_run": primary["id"]},
        )
    if not selection.version_ids:  # nothing matched the criteria: nothing to plan or send
        data = {"primary_run": primary["id"], "selected": 0, "selection": selection.by_reason}
        if state.json_output:
            write_json(result_payload("llm review", data))
        else:
            stdout_console().print(
                t("llm.review.nothing", run_id=primary["id"]), markup=False, highlight=False
            )
        raise typer.Exit(ExitCode.OK)

    def link(connection: sqlite3.Connection, opened: OpenedRun) -> dict[str, object]:
        if opened.run_id == primary["id"]:  # defensive: the run keys already differ
            raise AppError(
                "REVIEWER_SAME_AS_PRIMARY",
                ExitCode.CONFIG_ERROR,
                t("llm.review.error.same_as_primary"),
            )
        reviews.record(
            connection,
            primary["id"],
            opened.run_id,
            review_settings.mode,
            review_settings.criteria,
            review_settings.policy,
            selection,
            tuple(conversation or ()),
        )
        return {
            "review": {
                "primary_run": primary["id"],
                "mode": review_settings.mode.value,
                "criteria": [c.value for c in review_settings.criteria]
                if review_settings.mode is ReviewMode.SELECTIVE
                else [],
                "selected": len(selection.version_ids),
                "selection": selection.by_reason,
                "frozen_from_review_run": selection.frozen_from,
                "metrics_scoring_run": selection.scoring_run,
                "policy": review_settings.policy.value,
            }
        }

    run_configured(
        state,
        target,
        reviewer_settings,
        replace(args, versions=selection.version_ids),
        "llm review",
        prepared=prepared,
        on_opened=link,
    )


def _select(
    target: Workspace,
    settings: Settings,
    primary_run: int | None,
    conversation: list[str] | None,
    reviewer_run_key: str,
    reselect: bool,
) -> tuple[sqlite3.Row, reviews.Selection]:
    connection = open_database(target)
    try:
        primary = reviews.primary_run(
            connection, settings.llm.provider, settings.llm.model, primary_run
        )
        review_settings = settings.llm.review
        frozen = None
        if not reselect and not conversation:
            frozen = reviews.previous_selection(
                connection,
                primary["id"],
                review_settings.mode,
                review_settings.criteria,
                reviewer_run_key,
            )
        selection = frozen or reviews.select(
            connection,
            primary["id"],
            review_settings.mode,
            review_settings.criteria,
            tuple(conversation or ()),
        )
    except reviews.ReviewError as exc:
        raise review_error(exc) from exc
    finally:
        connection.close()
    return primary, selection


def consolidate(
    ctx: typer.Context,
    out: Annotated[Path, typer.Option(metavar="FILE", help=t("export.option.out"))],
    workspace: WorkspaceOption = None,
    config: Annotated[
        Path | None, typer.Option(metavar="FILE", help=t("llm.run.option.config"))
    ] = None,
    primary_run: Annotated[
        int | None, typer.Option(metavar="ID", help=t("llm.consolidate.option.primary_run"))
    ] = None,
    review_run: Annotated[
        int | None, typer.Option(metavar="ID", help=t("llm.consolidate.option.review_run"))
    ] = None,
    policy: Annotated[
        str | None, typer.Option(metavar="POLICY", help=t("llm.consolidate.option.policy"))
    ] = None,
    overwrite: Annotated[bool, typer.Option(help=t("export.option.overwrite"))] = False,
) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    chosen = load_settings(target, config).llm.review.policy
    if policy is not None:
        try:
            chosen = ConsolidationPolicy(policy)
        except ValueError as exc:
            raise AppError(
                "INVALID_OPTION",
                ExitCode.USAGE_ERROR,
                t(
                    "llm.consolidate.error.policy",
                    value=policy,
                    choices=", ".join(p.value for p in ConsolidationPolicy),
                ),
            ) from exc
    metadata = out.with_name(out.name + ".meta.json")
    for path in (out, metadata):
        if path.exists() and not overwrite:
            raise AppError(
                "OUTPUT_EXISTS",
                ExitCode.USAGE_ERROR,
                t("export.error.output_exists", path=path),
                hint=t("export.hint.overwrite"),
            )
    if out.is_dir() or not out.parent.is_dir():
        raise AppError(
            "OUTPUT_NOT_WRITABLE", ExitCode.OUTPUT_ERROR, t("export.error.invalid_path", path=out)
        )
    connection = open_database(target)
    try:
        link = reviews.latest_review(connection, primary_run, review_run)
        result = reviews.consolidate(connection, link, chosen)
        reviews.export(result, out)
    except reviews.ReviewError as exc:
        raise review_error(exc) from exc
    except OSError as exc:
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t("export.error.not_writable", path=out, reason=exc.strerror or type(exc).__name__),
        ) from exc
    finally:
        connection.close()
    data = {"path": str(out), "metadata_path": str(metadata), **result.summary}
    if state.json_output:
        write_json(result_payload("llm consolidate", data))
        return
    stdout_console().print(
        t(
            "llm.consolidate.written",
            rows=result.summary["rows"],
            reviewed=result.summary["reviewed"],
            human=result.summary["needs_human_review"],
            path=out,
        ),
        markup=False,
        highlight=False,
    )
    costs = result.summary["costs"]
    assert isinstance(costs, dict)
    primary_info, review_info = result.summary["primary_run"], result.summary["review_run"]
    assert isinstance(primary_info, dict) and isinstance(review_info, dict)
    stdout_console().print(
        t(
            "llm.consolidate.costs",
            primary_run=primary_info["id"],
            primary=costs["primary"] if costs["primary"] is not None else "-",
            review_run=review_info["id"],
            reviewer=costs["reviewer"] if costs["reviewer"] is not None else "-",
        ),
        markup=False,
        highlight=False,
    )


def review_error(exc: reviews.ReviewError) -> AppError:
    exit_code = {
        "PRIMARY_RUN_NOT_FOUND": ExitCode.USAGE_ERROR,
        "PRIMARY_RUN_UNFINISHED": ExitCode.USAGE_ERROR,
        "PRIMARY_IS_A_REVIEW": ExitCode.USAGE_ERROR,
        "REVIEW_NOT_FOUND": ExitCode.USAGE_ERROR,
    }[exc.code]
    return AppError(
        exc.code,
        exit_code,
        t(f"llm.review.error.{exc.code}", **{k: str(v) for k, v in exc.details.items()}),
        details={k: v for k, v in exc.details.items() if v is not None},
    )
