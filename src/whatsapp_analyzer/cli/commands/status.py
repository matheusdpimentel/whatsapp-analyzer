import typer
from rich.table import Table

from whatsapp_analyzer.cli.output import CliState, result_payload, stdout_console, write_json
from whatsapp_analyzer.cli.workspace_support import (
    WorkspaceOption,
    open_database,
    resolve_workspace,
)
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.metrics.reports import workspace_status
from whatsapp_analyzer.metrics.scoring import Level


def status(ctx: typer.Context, workspace: WorkspaceOption = None) -> None:
    state: CliState = ctx.obj
    target = resolve_workspace(workspace)
    connection = open_database(target)
    try:
        data = workspace_status(connection)
    finally:
        connection.close()
    data = {"workspace": str(target.root), **data}

    if state.json_output:
        write_json(result_payload("status", data))
        return
    _render(data)


def _render(data: dict[str, object]) -> None:
    console = stdout_console()
    conversations = data["conversations"]
    runs = data["runs"]
    assert isinstance(conversations, dict) and isinstance(runs, list)

    summary = Table(title=t("status.title"), title_justify="left", show_header=False)
    summary.add_column()
    summary.add_column(justify="right")
    by_format = conversations["by_format"]
    assert isinstance(by_format, dict)
    for key, value in (
        ("conversations", conversations["total"]),
        ("groups", conversations["groups"]),
        ("with_earlier_versions", conversations["with_earlier_versions"]),
        ("messages_current", data["messages_current"]),
        ("versions_superseded", data["versions_superseded"]),
        ("imports", data["imports"]),
        ("metric_configs", data["metric_configs"]),
        ("runs_total", data["runs_total"]),
    ):
        summary.add_row(t(f"status.{key}"), f"{value:,}")
    if by_format:
        summary.add_row(
            t("status.formats"), ", ".join(f"{k} ({v})" for k, v in sorted(by_format.items()))
        )
    console.print(summary)
    if conversations["keys_in_several_formats"]:
        console.print(
            t("status.warning.several_formats", count=conversations["keys_in_several_formats"]),
            style="yellow",
            highlight=False,
        )

    if not runs:
        console.print(t("status.no_runs"), highlight=False)
        return
    table = Table(title=t("status.runs.title"), title_justify="left")
    for name in ("id", "status", "reference_date", "scored", "data"):
        table.add_column(t(f"status.runs.column.{name}"))
    for level in Level:
        table.add_column(level.value, justify="right")
    for run in runs:
        assert isinstance(run, dict)
        levels = run["levels"]
        assert isinstance(levels, dict)
        table.add_row(
            str(run["id"]),
            t(f"status.run_status.{run['status']}"),
            str(run["reference_date"]),
            f"{run['conversations_scored']:,}/{run['conversations_total']:,}",
            t("status.data.current") if run["current_data"] else t("status.data.outdated"),
            *(f"{levels[level.value]:,}" for level in Level),
        )
    console.print(table)
