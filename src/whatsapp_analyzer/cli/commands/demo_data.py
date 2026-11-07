from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from whatsapp_analyzer.cli.output import CliState, result_payload, stdout_console, write_json
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.export_formats import Platform
from whatsapp_analyzer.i18n import t
from whatsapp_analyzer.synthetic.generator import GeneratorSettings
from whatsapp_analyzer.synthetic.writers import write_dataset


class ContentLocale(StrEnum):
    EN = "en"
    PT_BR = "pt-BR"


class PlatformChoice(StrEnum):
    ALL = "all"
    ANDROID = Platform.ANDROID.value
    IOS = Platform.IOS.value


def demo_data(
    ctx: typer.Context,
    output_dir: Annotated[
        Path,
        typer.Argument(metavar="OUTPUT_DIR", help=t("demo_data.arg.output_dir")),
    ],
    conversations: Annotated[
        int, typer.Option(min=0, help=t("demo_data.option.conversations"))
    ] = 10,
    seed: Annotated[int, typer.Option(help=t("demo_data.option.seed"))] = 7,
    locale: Annotated[
        ContentLocale,
        typer.Option(help=t("demo_data.option.locale")),
    ] = ContentLocale.EN,
    platform: Annotated[
        PlatformChoice,
        typer.Option(help=t("demo_data.option.platform")),
    ] = PlatformChoice.ALL,
    overwrite: Annotated[bool, typer.Option(help=t("demo_data.option.overwrite"))] = False,
) -> None:
    state: CliState = ctx.obj
    _check_output_dir(output_dir, overwrite)

    platforms = tuple(Platform) if platform is PlatformChoice.ALL else (Platform(platform.value),)
    settings = GeneratorSettings(seed=seed, conversations=conversations, locale=locale.value)
    try:
        summary = write_dataset(settings, output_dir, platforms)
    except OSError as exc:
        raise AppError(
            "OUTPUT_NOT_WRITABLE",
            ExitCode.OUTPUT_ERROR,
            t(
                "demo_data.error.not_writable",
                path=output_dir,
                reason=exc.strerror or type(exc).__name__,
            ),
        ) from exc

    if state.json_output:
        data: dict[str, object] = {
            "output_dir": str(output_dir),
            "synthetic": True,
            "seed": seed,
            "locale": locale.value,
            "conversations": summary.conversations,
            "messages": summary.messages,
            "files": summary.files,
        }
        write_json(result_payload("demo-data", data))
        return

    console = stdout_console()
    console.print(t("demo_data.done", path=output_dir), highlight=False)
    console.print(
        t(
            "demo_data.summary",
            conversations=summary.conversations,
            messages=summary.messages,
            files=len(summary.files),
        ),
        highlight=False,
    )
    console.print(t("demo_data.notice"), style="dim", highlight=False)


def _check_output_dir(output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists() and not output_dir.is_dir():
        raise AppError(
            "OUTPUT_NOT_A_DIRECTORY",
            ExitCode.USAGE_ERROR,
            t("demo_data.error.not_a_directory", path=output_dir),
        )
    if not overwrite and output_dir.is_dir() and any(output_dir.iterdir()):
        raise AppError(
            "OUTPUT_DIR_NOT_EMPTY",
            ExitCode.USAGE_ERROR,
            t("demo_data.error.not_empty", path=output_dir),
            hint=t("demo_data.error.not_empty_hint"),
        )
