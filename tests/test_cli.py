import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from whatsapp_analyzer import __version__, diagnostics
from whatsapp_analyzer.cli.app import main
from whatsapp_analyzer.diagnostics import Check, Status
from whatsapp_analyzer.exit_codes import ExitCode


def _fake_checks(statuses: list[Status]) -> list[Check]:
    return [
        Check(
            "python_version",
            status,
            "3.11.0",
            "doctor.check.python_version.ok",
            {"version": "3.11.0"},
        )
        for status in statuses
    ]


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostics, "run_checks", lambda path, environ: _fake_checks([Status.OK, Status.INFO])
    )


@pytest.fixture
def broken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostics, "run_checks", lambda path, environ: _fake_checks([Status.OK, Status.FAIL])
    )


def _json(captured: str) -> dict[str, object]:
    lines = captured.strip().splitlines()
    assert len(lines) == 1, "JSON mode must write exactly one document"
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == ExitCode.OK
    assert capsys.readouterr().out.strip() == f"whatsapp-analyzer {__version__}"


def test_help_lists_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "doctor" in out
    assert "Check the local environment" in out


def test_help_in_portuguese(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--lang", "pt-BR", "--help"]) == ExitCode.OK
    assert "Verifica o ambiente local" in capsys.readouterr().out


def test_language_from_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], healthy: None
) -> None:
    monkeypatch.setenv("WCA_LANG", "pt-BR")
    assert main(["doctor"]) == ExitCode.OK
    assert "Verificação do ambiente" in capsys.readouterr().out


def test_doctor_human_output(capsys: pytest.CaptureFixture[str], healthy: None) -> None:
    assert main(["doctor"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "Environment check" in out
    assert "1 ok, 0 failures" in out


def test_doctor_json_envelope(capsys: pytest.CaptureFixture[str], healthy: None) -> None:
    assert main(["--json", "doctor"]) == ExitCode.OK
    payload = _json(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["ok"] is True
    assert payload["command"] == "doctor"
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["summary"] == {"ok": 1, "fail": 0, "info": 1}
    assert data["checks"][0]["id"] == "python_version"
    assert "error" not in payload


def test_doctor_json_keeps_codes_in_english_when_localized(
    capsys: pytest.CaptureFixture[str], broken: None
) -> None:
    main(["--json", "--lang", "pt-BR", "doctor"])
    payload = _json(capsys.readouterr().out)
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "ENVIRONMENT_CHECK_FAILED"
    assert error["message"] == "Uma ou mais verificações do ambiente falharam."


def test_doctor_failure_sets_exit_code(capsys: pytest.CaptureFixture[str], broken: None) -> None:
    assert main(["--json", "doctor"]) == ExitCode.ENVIRONMENT_CHECK_FAILED
    payload = _json(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == {
        "code": "ENVIRONMENT_CHECK_FAILED",
        "message": "One or more environment checks failed.",
        "exit_code": 9,
    }


def test_doctor_with_real_checks(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "doctor"]) == ExitCode.OK
    payload = _json(capsys.readouterr().out)
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["summary"]["fail"] == 0


def test_unsupported_language_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "--lang", "fr", "doctor"]) == ExitCode.USAGE_ERROR
    payload = _json(capsys.readouterr().out)
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "USAGE_ERROR"
    assert "fr" in str(error["message"])


def test_unknown_command_human_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["frobnicate"]) == ExitCode.USAGE_ERROR
    assert "frobnicate" in capsys.readouterr().err


def test_unknown_command_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "frobnicate"]) == ExitCode.USAGE_ERROR
    payload = _json(capsys.readouterr().out)
    assert payload["command"] == "frobnicate"


def test_internal_errors_hide_exception_messages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(path: Path, environ: object) -> list[Check]:
        raise RuntimeError("private message content must not leak")

    monkeypatch.setattr(diagnostics, "run_checks", explode)
    assert main(["--json", "doctor"]) == ExitCode.INTERNAL_ERROR
    captured = capsys.readouterr()
    assert "private message content" not in captured.out + captured.err
    payload = _json(captured.out)
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "INTERNAL_ERROR"
    assert "RuntimeError" in str(error["message"])


def test_debug_mode_reraises_internal_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(path: Path, environ: object) -> list[Check]:
        raise RuntimeError("boom")

    monkeypatch.setattr(diagnostics, "run_checks", explode)
    monkeypatch.setenv("WCA_DEBUG", "1")
    with pytest.raises(RuntimeError, match="boom"):
        main(["doctor"])


def test_no_arguments_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == ExitCode.OK
    assert "doctor" in capsys.readouterr().out


def test_json_without_command_is_a_json_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json"]) == ExitCode.USAGE_ERROR
    payload = _json(capsys.readouterr().out)
    assert payload["command"] is None
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "USAGE_ERROR"


def test_global_options_after_the_command_are_rejected_in_one_language(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--json", "doctor", "--lang", "pt-BR"]) == ExitCode.USAGE_ERROR
    error = _json(capsys.readouterr().out)["error"]
    assert isinstance(error, dict)
    assert "--lang" in str(error["message"])
    assert str(error["hint"]).startswith("Run ")


def test_human_output_survives_an_ascii_only_stream() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "whatsapp_analyzer", "--lang", "pt-BR", "doctor"],
        capture_output=True,
        text=True,
        encoding="ascii",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
        check=False,
    )
    assert completed.returncode == ExitCode.OK, completed.stderr
    assert "Verifica" in completed.stdout


def test_unknown_command_has_hint(capsys: pytest.CaptureFixture[str]) -> None:
    main(["frobnicate"])
    assert "whatsapp-analyzer --help" in capsys.readouterr().err


def test_human_errors_are_localized(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--lang", "pt-BR", "frobnicate"])
    err = capsys.readouterr().err
    assert err.startswith("Erro (USAGE_ERROR)")
    assert "para ver os comandos" in err


def test_python_module_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "whatsapp_analyzer", "--json", "doctor"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == ExitCode.OK
    assert json.loads(completed.stdout)["command"] == "doctor"
