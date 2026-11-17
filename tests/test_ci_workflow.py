"""Regression for the Windows package CI job: uv needs Scripts/python.exe."""

from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_package_job_uses_windows_executable_paths() -> None:
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    package = text[text.index("  package:") :]
    assert 'PY="$VENV/Scripts/python.exe"' in package
    assert 'EXE="$VENV/Scripts/whatsapp-analyzer.exe"' in package
    assert 'uv pip install --python "$PY"' in package
    assert '"$BIN/python"' not in package, "the extension-less Windows path is back"
