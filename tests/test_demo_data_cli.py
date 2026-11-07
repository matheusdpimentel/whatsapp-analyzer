import json
from pathlib import Path

import pytest

from whatsapp_analyzer.cli.app import main
from whatsapp_analyzer.exit_codes import ExitCode


def _run_json(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = main(["--json", *args])
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return code, payload


def _error_code(payload: dict[str, object]) -> object:
    error = payload["error"]
    assert isinstance(error, dict)
    return error["code"]


def test_generates_a_dataset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_json(["demo-data", str(tmp_path / "out"), "--conversations", "3"], capsys)
    assert code == ExitCode.OK
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["synthetic"] is True
    assert data["conversations"] == 5  # three requested plus two edge cases
    assert "conversations.csv" in data["files"]
    assert (tmp_path / "out" / "txt" / "android-en" / "chat_0001.txt").is_file()


def test_portuguese_content_and_single_platform(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    code, _ = _run_json(
        ["demo-data", str(out), "--locale", "pt-BR", "--platform", "ios", "--conversations", "1"],
        capsys,
    )
    assert code == ExitCode.OK
    assert [p.name for p in (out / "txt").iterdir()] == ["ios-pt-BR"]


def test_refuses_a_non_empty_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "keep.txt").write_text("user file", encoding="utf-8")
    code, payload = _run_json(["demo-data", str(tmp_path)], capsys)
    assert code == ExitCode.USAGE_ERROR
    assert _error_code(payload) == "OUTPUT_DIR_NOT_EMPTY"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "user file"


def test_overwrite_writes_into_a_non_empty_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "keep.txt").write_text("user file", encoding="utf-8")
    code, _ = _run_json(["demo-data", str(tmp_path), "--overwrite", "--conversations", "1"], capsys)
    assert code == ExitCode.OK
    assert (tmp_path / "keep.txt").exists()
    assert (tmp_path / "manifest.json").exists()


def test_refuses_a_file_as_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "file.txt"
    target.write_text("", encoding="utf-8")
    code, payload = _run_json(["demo-data", str(target)], capsys)
    assert code == ExitCode.USAGE_ERROR
    assert _error_code(payload) == "OUTPUT_NOT_A_DIRECTORY"


def test_rejects_unknown_locale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_json(["demo-data", str(tmp_path / "out"), "--locale", "fr"], capsys)
    assert code == ExitCode.USAGE_ERROR
    assert _error_code(payload) == "USAGE_ERROR"


def test_human_output_in_portuguese(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["--lang", "pt-BR", "demo-data", str(tmp_path / "out"), "--conversations", "1"]) == 0
    )
    out = capsys.readouterr().out
    assert "3 conversas" in out
    assert "fictícios" in out


def test_unwritable_output_has_its_own_error_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file.txt"
    blocker.write_text("", encoding="utf-8")
    code, payload = _run_json(["demo-data", str(blocker / "sub")], capsys)
    assert code == ExitCode.OUTPUT_ERROR
    assert _error_code(payload) == "OUTPUT_NOT_WRITABLE"


def test_locale_values_are_case_sensitive_as_documented(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_json(["demo-data", str(tmp_path / "out"), "--locale", "pt-br"], capsys)
    assert code == ExitCode.USAGE_ERROR
    error = payload["error"]
    assert isinstance(error, dict)
    assert "pt-BR" in str(error["message"])
