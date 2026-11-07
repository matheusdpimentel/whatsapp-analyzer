"""End-to-end tests of `whatsapp-analyzer import` with synthetic data."""

import json
from pathlib import Path

import pytest

from whatsapp_analyzer.cli.app import main
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.i18n import load_catalog
from whatsapp_analyzer.ingest.model import InputErrorCode, IssueCode, Message
from whatsapp_analyzer.ingest.pipeline import ImportOptions, run_import
from whatsapp_analyzer.ingest.whatsapp_txt import DateOrder
from whatsapp_analyzer.synthetic.generator import GeneratorSettings
from whatsapp_analyzer.synthetic.writers import write_dataset

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run_json(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = main(["--json", *args])
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return code, payload


def messages_of(path: Path, **options: object) -> list[Message]:
    collected: list[Message] = []
    report = run_import([path], ImportOptions(**options), sink=collected.append)  # type: ignore[arg-type]
    assert not report.rejected_files
    return collected


@pytest.mark.parametrize("format_dir", ["android-en", "ios-en"])
def test_txt_to_normalized_csv_and_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], format_dir: str
) -> None:
    write_dataset(GeneratorSettings(seed=21, conversations=7), tmp_path / "data")
    source = tmp_path / "data" / "txt" / format_dir
    output = tmp_path / "normalized.csv"
    args = ["import", str(source), "--owner", "Northwind Support", "--date-order", "mdy"]

    code, payload = run_json([*args, "--normalized-out", str(output)], capsys)
    assert code == ExitCode.OK, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["output"] == {"path": str(output), "records": data["totals"]["records_written"]}

    original = messages_of(source, owner="Northwind Support", date_order=DateOrder.MONTH_FIRST)
    reimported = messages_of(output)
    assert len(reimported) == len(original)
    assert [m.key() for m in reimported] == [m.key() for m in original]


def test_rejected_file_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "normalized.csv"
    code, payload = run_json(
        [
            "import",
            str(EXAMPLES / "demo-en" / "txt" / "android-en"),
            "--owner",
            "Northwind Support",
            "--normalized-out",
            str(output),
        ],
        capsys,
    )
    assert code == ExitCode.VALIDATION_FAILED
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "AMBIGUOUS_DATE_ORDER"
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_report_hides_file_names_unless_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = str(EXAMPLES / "demo-en" / "conversations.csv")
    _, hidden = run_json(["import", source], capsys)
    _, shown = run_json(["import", source, "--show-file-names"], capsys)
    assert "name" not in hidden["data"]["files"][0]  # type: ignore[index]
    assert shown["data"]["files"][0]["name"] == source  # type: ignore[index]


def test_report_never_contains_message_text(capsys: pytest.CaptureFixture[str]) -> None:
    source = EXAMPLES / "demo-en" / "conversations.csv"
    main(["import", str(source)])
    main(["--json", "import", str(source)])
    output = capsys.readouterr()
    for phrase in ("order number", "tracking.example.com", "Northwind Support"):
        assert phrase not in output.out + output.err


def test_existing_output_needs_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out.csv"
    output.write_text("keep", encoding="utf-8")
    source = str(EXAMPLES / "demo-en" / "conversations.csv")
    code, payload = run_json(["import", source, "--normalized-out", str(output)], capsys)
    assert code == ExitCode.USAGE_ERROR
    assert payload["error"]["code"] == "OUTPUT_EXISTS"  # type: ignore[index]
    assert output.read_text(encoding="utf-8") == "keep"
    code, _ = run_json(["import", source, "--normalized-out", str(output), "--overwrite"], capsys)
    assert code == ExitCode.OK
    assert output.read_text(encoding="utf-8").startswith("conversation_id,")


def test_strict_fails_on_invalid_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "data.csv"
    lines = (EXAMPLES / "demo-en" / "conversations.csv").read_text(encoding="utf-8").splitlines()
    source.write_text(
        "\n".join([*lines[:3], "chat_9999,not-a-date,A,inbound,x,text,"]) + "\n", encoding="utf-8"
    )
    assert run_json(["import", str(source)], capsys)[0] == ExitCode.OK
    code, payload = run_json(["import", str(source), "--strict"], capsys)
    assert code == ExitCode.VALIDATION_FAILED
    assert payload["error"]["code"] == "INVALID_RECORDS"  # type: ignore[index]


def test_mapping_preset_and_file(capsys: pytest.CaptureFixture[str]) -> None:
    custom = str(EXAMPLES / "demo-en" / "conversations_custom_columns.csv")
    assert run_json(["import", custom], capsys)[0] == ExitCode.VALIDATION_FAILED
    mapping = str(EXAMPLES / "mappings" / "custom-columns.toml")
    assert run_json(["import", custom, "--mapping", mapping], capsys)[0] == ExitCode.OK
    canonical = str(EXAMPLES / "demo-en" / "conversations.csv")
    assert run_json(["import", canonical, "--mapping", "canonical"], capsys)[0] == ExitCode.OK


def test_bad_mapping_arguments_are_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = str(EXAMPLES / "demo-en" / "conversations.csv")
    code, payload = run_json(["import", source, "--map", "nonsense"], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "INVALID_COLUMN_MAPPING")  # type: ignore[index]
    code, payload = run_json(
        ["import", source, "--mapping", str(tmp_path / "missing.toml")], capsys
    )
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "FILE_NOT_FOUND")  # type: ignore[index]


def test_human_report_in_portuguese(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["--lang", "pt-BR", "import", str(EXAMPLES / "demo-pt-BR" / "conversations.csv")]) == 0
    )
    out = capsys.readouterr().out
    assert "Resumo da importação" in out
    assert "Registros válidos" in out


def test_issue_and_error_codes_have_messages() -> None:
    catalog = load_catalog("en")
    assert all(f"import.issue.{code.value}" in catalog for code in IssueCode)
    assert all(f"import.input_error.{code.value}" in catalog for code in InputErrorCode)


def test_no_input_files_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out.csv"
    (tmp_path / "empty").mkdir()
    code, payload = run_json(
        ["import", str(tmp_path / "empty"), "--normalized-out", str(output)], capsys
    )
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "NO_INPUT_FILES")  # type: ignore[index]
    assert not output.exists()


def test_failure_to_move_the_output_into_place_is_an_output_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "replace", refuse)
    output = tmp_path / "out.csv"
    source = str(EXAMPLES / "demo-en" / "conversations.csv")
    code, payload = run_json(["import", source, "--normalized-out", str(output)], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.OUTPUT_ERROR, "OUTPUT_NOT_WRITABLE")  # type: ignore[index]
    assert list(tmp_path.iterdir()) == []
