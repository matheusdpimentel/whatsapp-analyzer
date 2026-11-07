from pathlib import Path

import pytest

from whatsapp_analyzer import diagnostics
from whatsapp_analyzer.diagnostics import Check, Status


def test_python_check_reports_the_version() -> None:
    check = diagnostics.check_python((3, 12, 4))
    assert check.status is Status.OK
    assert check.value == "3.12.4"


def test_cpu_count_unavailable_is_reported_as_null() -> None:
    check = diagnostics.check_cpu_count(None)
    assert check.value is None
    assert check.message_key.endswith("unavailable")


def test_cpu_count_is_reported() -> None:
    assert diagnostics.check_cpu_count(8).params == {"count": 8}


def test_memory_unavailable_is_null_not_zero() -> None:
    check = diagnostics.check_memory(None)
    assert check.value is None
    assert check.message_key.endswith("unavailable")


def test_memory_is_reported_in_bytes_and_gib() -> None:
    check = diagnostics.check_memory(16 * 1024**3)
    assert check.value == 16 * 1024**3
    assert check.params == {"gib": "16.0"}


def test_disk_check_reports_free_bytes(tmp_path: Path) -> None:
    check = diagnostics.check_disk(tmp_path)
    assert isinstance(check.value, dict)
    assert check.value["free_bytes"] > 0


def test_disk_check_on_missing_directory_is_unavailable(tmp_path: Path) -> None:
    check = diagnostics.check_disk(tmp_path / "deleted")
    assert check.status is Status.INFO
    assert check.value is None
    assert check.message_key.endswith("unavailable")


def test_sqlite_probe_supports_wal() -> None:
    assert diagnostics.sqlite_supports_wal() is True


def test_sqlite_check_fails_without_wal() -> None:
    assert diagnostics.check_sqlite(wal_supported=False).status is Status.FAIL


def test_credentials_check_never_exposes_secret_values() -> None:
    secret = "sk-test-not-a-real-key-123456"
    check = diagnostics.check_credentials({"OPENAI_API_KEY": secret, "ANTHROPIC_API_KEY": ""})
    assert check.value == {
        "OPENAI_API_KEY": True,
        "ANTHROPIC_API_KEY": False,
        "OPENAI_COMPAT_API_KEY": False,
    }
    assert secret not in repr(check)


def test_credentials_check_without_keys() -> None:
    check = diagnostics.check_credentials({})
    assert check.message_key == "doctor.check.credentials.none"


def test_provider_sdks_check_lists_only_installed_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "anthropic" else None,
    )
    assert diagnostics.check_provider_sdks().value == ["anthropic"]


def test_summarize_counts_every_status() -> None:
    checks = [
        Check("a", Status.OK, None, "k"),
        Check("b", Status.FAIL, None, "k"),
        Check("c", Status.INFO, None, "k"),
        Check("d", Status.INFO, None, "k"),
    ]
    assert diagnostics.summarize(checks) == {"ok": 1, "fail": 1, "info": 2}


def test_run_checks_returns_every_check_once(tmp_path: Path) -> None:
    ids = [check.id for check in diagnostics.run_checks(tmp_path, {})]
    assert ids == [
        "python_version",
        "platform",
        "cpu_count",
        "memory_total",
        "disk_free",
        "sqlite",
        "provider_sdks",
        "credentials",
    ]
