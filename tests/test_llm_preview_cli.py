"""`llm prompts` and `llm preview`: offline preview, confirmation and safe content output."""

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import data_of, exchange, new_workspace, row, run_cli, write_csv
from whatsapp_analyzer.cli.app import main
from whatsapp_analyzer.cli.commands import llm as llm_command
from whatsapp_analyzer.exit_codes import ExitCode

PRIVATE = "Synthetic private sentence 91c2"


def populated(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    workspace = new_workspace(tmp_path)
    rows = [
        *exchange("alpha", 5),
        row("alpha", "2026-01-06T10:00:00", "inbound", PRIVATE),
        row("team", "2026-01-05T10:00:00", "inbound", "a", sender="First"),
        row("team", "2026-01-05T10:01:00", "inbound", "b", sender="Second"),
    ]
    run_cli(["import", str(write_csv(tmp_path / "a.csv", rows)), "-w", str(workspace)], capsys)
    return workspace


def preview(
    workspace: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> tuple[int, dict[str, object]]:
    return run_cli(["llm", "preview", "alpha", "-w", str(workspace), *extra], capsys)


def error_code(payload: dict[str, object]) -> str:
    error = payload["error"]
    assert isinstance(error, dict)
    return str(error["code"])


def test_prompts_lists_builtin_templates(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = run_cli(["llm", "prompts"], capsys)
    templates = data_of(payload)["templates"]
    assert code == 0 and isinstance(templates, list)
    assert templates[0]["id"] == "engagement-classification"


def test_default_preview_is_structure_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    code, payload = preview(workspace, capsys, "--simulate")
    data = data_of(payload)
    assert code == 0 and payload["command"] == "llm preview"
    assert "content" not in data and PRIVATE not in str(payload)
    tokens = data["tokens"]
    assert isinstance(tokens, dict)
    assert (tokens["max_output"], tokens["estimated"]) == (300, True)
    assert tokens["input_estimated"] > 0
    assert data["simulation"]["status"] == "SUCCESS"  # type: ignore[index]
    assert data["cost_upper_bound"]["total"] == "0"  # type: ignore[index]


def test_content_needs_explicit_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    code, payload = preview(workspace, capsys, "--show-content")
    assert (code, error_code(payload)) == (ExitCode.USAGE_ERROR, "CONTENT_CONFIRMATION_REQUIRED")
    code, payload = preview(workspace, capsys, "--show-content", "--confirm-content")
    content = data_of(payload)["content"]
    assert code == 0 and isinstance(content, str)
    assert PRIVATE in content and "alpha contact" not in content  # masked, not anonymized


def test_out_file_is_private_and_never_silently_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    out = workspace / "preview.md"
    code, payload = preview(workspace, capsys, "--out", str(out))
    assert error_code(payload) == "CONTENT_CONFIRMATION_REQUIRED" and not out.exists()
    code, payload = preview(workspace, capsys, "--out", str(out), "--confirm-content")
    assert code == 0 and PRIVATE in out.read_text(encoding="utf-8")
    assert "content" not in data_of(payload)
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
    code, payload = preview(workspace, capsys, "--out", str(out), "--confirm-content")
    assert error_code(payload) == "OUTPUT_EXISTS"
    code, _ = preview(workspace, capsys, "--out", str(out), "--confirm-content", "--overwrite")
    assert code == 0
    if sys.platform != "win32":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_out_refuses_git_work_trees_but_allows_workspaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    workspace = populated(repo, capsys)  # workspace inside the repository
    target = repo / "leak.md"
    code, payload = preview(workspace, capsys, "--out", str(target), "--confirm-content")
    assert (code, error_code(payload)) == (ExitCode.USAGE_ERROR, "OUTPUT_IN_GIT_WORKTREE")
    assert not target.exists()
    code, _ = preview(workspace, capsys, "--out", str(workspace / "ok.md"), "--confirm-content")
    assert code == 0
    code, _ = preview(
        workspace, capsys, "--out", str(target), "--confirm-content", "--allow-git-worktree"
    )
    assert code == 0


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_out_refuses_symlinks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = populated(tmp_path, capsys)
    (workspace / "real.md").write_text("x", encoding="utf-8")
    link = workspace / "link.md"
    link.symlink_to(workspace / "real.md")
    _, payload = preview(workspace, capsys, "--out", str(link), "--confirm-content", "--overwrite")
    assert error_code(payload) == "OUTPUT_IS_SYMLINK"
    assert (workspace / "real.md").read_text(encoding="utf-8") == "x"


def test_groups_unknown_and_ambiguous_conversations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    _, payload = run_cli(["llm", "preview", "team", "-w", str(workspace)], capsys)
    assert error_code(payload) == "GROUP_EXCLUDED"
    _, payload = run_cli(["llm", "preview", "nobody", "-w", str(workspace)], capsys)
    assert error_code(payload) == "CONVERSATION_NOT_FOUND"


def test_custom_prompt_and_invalid_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    bad = tmp_path / "bad.md"
    bad.write_text("no front matter", encoding="utf-8")
    code, payload = preview(workspace, capsys, "--prompt", str(bad))
    assert (code, error_code(payload)) == (ExitCode.CONFIG_ERROR, "PROMPT_INVALID")
    code, payload = preview(workspace, capsys, "--prompt", "missing-template")
    assert (code, error_code(payload)) == (ExitCode.CONFIG_ERROR, "PROMPT_NOT_FOUND")


def test_workspace_pricing_without_cache_prices_gives_upper_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys)
    (workspace / "pricing.toml").write_text(
        'schema = 1\n[[prices]]\nprovider = "fake"\nmodel = "fake-classifier"\ncurrency = "USD"\n'
        'unit = "per_million_tokens"\ninput = "1.00"\noutput = "2.00"\n'
        'effective_from = 2026-06-01\nsource = "local test"\n',
        encoding="utf-8",
    )
    _, payload = preview(workspace, capsys)
    cost = data_of(payload)["cost_upper_bound"]
    assert isinstance(cost, dict) and cost["total"] is not None and cost["missing"] == []
    (workspace / "pricing.toml").write_text(
        "schema = 1\n[[prices]]\ninput = 1.5\n", encoding="utf-8"
    )
    code, payload = preview(workspace, capsys)
    assert (code, error_code(payload)) == (ExitCode.CONFIG_ERROR, "CONFIG_INVALID")


def test_human_output_in_portuguese(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = populated(tmp_path, capsys)
    assert main(["--lang", "pt-BR", "llm", "preview", "alpha", "-w", str(workspace)]) == 0
    captured = capsys.readouterr()
    assert "Requisição preparada (não enviada)" in captured.out
    assert "não torna o texto anônimo" in captured.err
    assert PRIVATE not in captured.out + captured.err


def test_human_content_output_is_exactly_the_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    rows = [row("beta", "2026-01-05T10:00:00", "inbound", "great :thumbs_up: [bold]x[/bold]")]
    run_cli(["import", str(write_csv(tmp_path / "b.csv", rows)), "-w", str(workspace)], capsys)
    assert (
        main(
            ["llm", "preview", "beta", "-w", str(workspace), "--show-content", "--confirm-content"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert ":thumbs_up:" in out and "&#91;bold]x&#91;/bold]" in out


def test_without_usable_git_any_git_marker_is_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert not llm_command.git_would_ignore(tmp_path / "repo", "x.md")
    assert llm_command.git_would_ignore(tmp_path, "x.md")
