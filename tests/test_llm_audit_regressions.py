"""Regression tests for the independent audit of milestone 4 (findings M4-01 to M4-07).

All data is synthetic. Each test failed before its fix.
"""

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from helpers import data_of, exchange, new_workspace, run_cli, write_csv
from whatsapp_analyzer.cli.commands import llm as llm_command
from whatsapp_analyzer.config import ContextSettings
from whatsapp_analyzer.errors import AppError
from whatsapp_analyzer.llm import output as output_module
from whatsapp_analyzer.llm.context import build_context
from whatsapp_analyzer.llm.masking import Masker
from whatsapp_analyzer.llm.prompts import find_template
from whatsapp_analyzer.metrics.raw import StoredMessage
from whatsapp_analyzer.schema import Direction, MessageType

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def mask(text: str, contacts: tuple[str, ...] = ()) -> str:
    return Masker(contacts, ()).mask(text)


def digits_left(text: str) -> str:
    """Digits outside placeholders."""
    return "".join(ch for ch in re.sub(r"\[[A-Z_]+_\d+\]", "", text) if ch.isdigit())


# M4-01: phones with Unicode digits, non-breaking spaces, other dashes, adjacent letters --------


@pytest.mark.parametrize(
    "text",
    [
        # full-width digits
        "tel \uff14\uff18 \uff19\uff19\uff19\uff19\uff19-\uff11\uff11\uff11\uff11",
        "48\u00a099999-1111",  # no-break space
        "48\u202f99999\u20111111",  # narrow no-break space, non-breaking hyphen
        "fone:48\u201399999\u20131111",  # en dashes
        "\uff0b55 48 99999-1111",  # full-width plus
        "tel48999991111",  # letters before
        "48999991111abc",  # letters after
        "ramal x48999991111y",
        "\u0664\u0668\u0669\u0669\u0669\u0669\u0669\u0661\u0661\u0661\u0661",  # Arabic-Indic digits
    ],
)
def test_m4_01_phone_variants_leave_no_digits(text: str) -> None:
    masked = mask(text)
    assert digits_left(masked) == "", masked


def test_m4_01_ordinary_numbers_next_to_letters_stay() -> None:
    assert mask("3x ao dia, 2a vez, v12") == "3x ao dia, 2a vez, v12"


# M4-02: masking and escaping must not undo each other ----------------------------------------


def prepared(text: str, sender: str) -> str:
    message = StoredMessage(
        0, datetime(2026, 1, 1), sender, Direction.INBOUND, text, MessageType.TEXT, None
    )
    context = build_context([message], ContextSettings(), is_group=False, ignored_texts=())
    return context.text.split("CONTACT: ", 1)[1]


@pytest.mark.parametrize(
    ("sender", "text", "leaked"),
    [
        ("Ana & Bia", "fala Ana & Bia", ("Ana", "Bia")),
        ("Jo\u00e3o <Vendas>", "oi Jo\u00e3o <Vendas>", ("Jo\u00e3o", "Vendas")),
        ("Bia [Loja]", "Bia [Loja] aqui", ("Bia", "Loja")),
        ("Amp Silva", "a & b, Amp", ("Amp", "Silva")),
        ("Lt Gomes", "x < y, Lt Gomes", ("Gomes",)),
    ],
)
def test_m4_02_known_names_are_masked_and_entities_stay_intact(
    sender: str, text: str, leaked: tuple[str, ...]
) -> None:
    body = prepared(text, sender)
    for part in leaked:
        assert part not in body, body
    # Every & starts a complete entity; no entity was broken by a placeholder.
    assert re.sub(r"&(?:amp|lt|gt|#91);", "", body).count("&") == 0, body


def test_m4_02_content_resembling_placeholders_cannot_collide() -> None:
    body = prepared("call me: [PHONE_1] or \ue000\ue001 or 48 99999-1111", "Someone")
    assert body.count("[PHONE_1]") == 1  # only the real placeholder
    assert "\ue000" not in body and "\ue001" not in body


# M4-03: a domain is masked whole or not at all -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "site.com.brx",
        "acesse loja.example.com.evil ok",
        "mail.site.com.br2",
        "x.com.br-foo",
        "painel.site.com.br.internal/x",
    ],
)
def test_m4_03_no_partial_domains(text: str) -> None:
    masked = mask(text)
    assert "[URL_1]" in masked, masked
    # Nothing of the host name is left next to the placeholder.
    assert re.search(r"\[URL_\d+\][.\-]?[A-Za-z0-9]", masked) is None, masked


# M4-04 and M4-05: template resolution -----------------------------------------------------------


def workspace_with_chat(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    workspace = new_workspace(tmp_path)
    run_cli(
        ["import", str(write_csv(tmp_path / "a.csv", exchange("alpha", 5))), "-w", str(workspace)],
        capsys,
    )
    return workspace


def test_m4_04_an_invalid_unselected_template_does_not_block_valid_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = workspace_with_chat(tmp_path, capsys)
    (workspace / "prompts").mkdir()
    (workspace / "prompts" / "broken.md").write_text("not a template", encoding="utf-8")
    assert find_template("engagement-classification", workspace / "prompts").origin == "built-in"
    code, _ = run_cli(["llm", "preview", "alpha", "-w", str(workspace)], capsys)
    assert code == 0
    code, payload = run_cli(["llm", "prompts", "-w", str(workspace)], capsys)
    data = data_of(payload)
    assert code == 0
    assert data["invalid"] == [{"file": "broken.md", "error": "missing_front_matter"}]


def test_m4_05_relative_prompt_path_in_config_is_workspace_relative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = workspace_with_chat(tmp_path, capsys)
    builtin = find_template(None)
    source = (
        Path(__file__).resolve().parent.parent
        / "src/whatsapp_analyzer/llm/prompts/engagement-classification/v1.md"
    )
    (workspace / "custom.md").write_text(
        source.read_text(encoding="utf-8").replace(
            'id = "engagement-classification"', 'id = "custom"'
        ),
        encoding="utf-8",
    )
    config = (workspace / "analyzer.toml").read_text(encoding="utf-8")
    (workspace / "analyzer.toml").write_text(
        config.replace(f'prompt = "{builtin.id}"', 'prompt = "custom.md"'), encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, payload = run_cli(["llm", "preview", "alpha", "-w", str(workspace)], capsys)
    assert code == 0, payload
    assert data_of(payload)["template"]["id"] == "custom"  # type: ignore[index]


# M4-06: Git's own rules decide whether --out is safe ---------------------------------------------


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [GIT or "git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


@needs_git
@pytest.mark.parametrize(
    ("gitignore", "target"),
    [
        ("*\n!preview.md\n", "preview.md"),  # negation un-ignores the file
        ("*\n!*.md\n", "preview.md"),
    ],
)
def test_m4_06_negated_rules_in_a_workspace_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], gitignore: str, target: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    workspace = workspace_with_chat(repo, capsys)
    (workspace / ".gitignore").write_text(gitignore, encoding="utf-8")
    code, payload = run_cli(
        [
            "llm",
            "preview",
            "alpha",
            "-w",
            str(workspace),
            "--out",
            str(workspace / target),
            "--confirm-content",
        ],
        capsys,
    )
    assert code != 0 and payload["error"]["code"] == "OUTPUT_IN_GIT_WORKTREE"  # type: ignore[index]
    assert not (workspace / target).exists()


@needs_git
def test_m4_06_tracked_files_are_refused_even_if_ignored_later(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "notes.md").write_text("x", encoding="utf-8")
    git(repo, "add", "notes.md")
    (repo / ".gitignore").write_text("notes.md\n", encoding="utf-8")
    workspace = workspace_with_chat(tmp_path, capsys)
    _, payload = run_cli(
        [
            "llm",
            "preview",
            "alpha",
            "-w",
            str(workspace),
            "--out",
            str(repo / "notes.md"),
            "--confirm-content",
            "--overwrite",
        ],
        capsys,
    )
    assert payload["error"]["code"] == "OUTPUT_IN_GIT_WORKTREE"  # type: ignore[index]
    assert (repo / "notes.md").read_text(encoding="utf-8") == "x"


@needs_git
def test_m4_06_ignored_locations_are_allowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    workspace = workspace_with_chat(repo, capsys)  # .gitignore "*"
    code, _ = run_cli(
        [
            "llm",
            "preview",
            "alpha",
            "-w",
            str(workspace),
            "--out",
            str(workspace / "p.md"),
            "--confirm-content",
        ],
        capsys,
    )
    assert code == 0


# M4-07: writing is bound to the directory that was checked ---------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or os.open not in os.supports_dir_fd, reason="POSIX dir_fd"
)
def test_m4_07_directory_swapped_after_the_check_is_detected(tmp_path: Path) -> None:
    safe = tmp_path / "safe"
    safe.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = llm_command.check_output_path(
        safe / "out.md", overwrite=False, allow_git_worktree=True
    )
    # The checked directory is replaced by a link to another place before writing.
    safe.rename(tmp_path / "moved")
    safe.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(AppError) as info:
        llm_command.write_private_file(target, "content", overwrite=False)
    assert info.value.code == "OUTPUT_LOCATION_CHANGED"
    assert not (elsewhere / "out.md").exists()


# Extra check: the output schema cannot be mutated globally ---------------------------------------


def test_output_schema_is_a_fresh_copy() -> None:
    schema = output_module.output_schema()
    schema["properties"]["level"]["enum"].append("HACKED")  # type: ignore[index]
    assert "HACKED" not in output_module.output_schema()["properties"]["level"]["enum"]  # type: ignore[index]


# Second audit: R-01, R-02, R-03 and residual M4-01 ---------------------------------------------

ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u180e")


def through_context(text: str, sender: str = "Someone", max_chars: int = 5000) -> str:
    message = StoredMessage(
        0, datetime(2026, 1, 1), sender, Direction.INBOUND, text, MessageType.TEXT, None
    )
    settings = ContextSettings(max_message_chars=max_chars)
    context = build_context([message], settings, is_group=False, ignored_texts=())
    return context.text.split("CONTACT: ", 1)[1]


@pytest.mark.parametrize(
    "prefix",
    [
        "e\ue000\u0301 " * 12,  # reserved char between a letter and its combining accent
        "a\ue001\u0308 " * 5,
        "\ue000\u0301" * 20,
        "o\u200b\u0303 " * 8,  # zero-width char between a letter and its accent
    ],
)
def test_r_01_names_are_found_after_any_removal_and_normalization(prefix: str) -> None:
    body = through_context(prefix + "Joao&Silva", sender="Joao&Silva")
    assert "Joao" not in body and "Silva" not in body, ascii(body)
    assert body.endswith("[CONTACT_NAME]&amp;[CONTACT_NAME]") or body.endswith("[CONTACT_NAME]"), (
        ascii(body)
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/reset?email=ana@example.com&token=PRIVATESUFFIX7f3a",
        "https://ana@example.org/private/PRIVATESUFFIX7f3a",
        "https://ana:pw@example.org/x?PRIVATESUFFIX7f3a",
        "https://example.org/u/ana@example.com/PRIVATESUFFIX7f3a",
        "example.org/reset?email=ana@example.com&token=PRIVATESUFFIX7f3a",
        "www.example.org/?to=ana@example.com#PRIVATESUFFIX7f3a",
    ],
)
def test_r_02_urls_containing_emails_become_one_placeholder(url: str) -> None:
    body = through_context(f"see {url} now")
    assert body == "see [URL_1] now", ascii(body)


def test_r_02_plain_emails_and_hosts_are_unchanged_in_behavior() -> None:
    assert (
        through_context("mail ana.lima@example.com or joao.com@gmail.com")
        == "mail [EMAIL_1] or [EMAIL_2]"
    )
    assert through_context("price 10.50/kg, v1.2.3, e.g. ok") == "price 10.50/kg, v1.2.3, e.g. ok"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX dir_fd")
def test_r_03_protected_write_is_selected_on_this_platform() -> None:
    assert llm_command.dir_fd_writes_supported()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX dir_fd")
@pytest.mark.parametrize("overwrite", [False, True])
def test_r_03_parent_swapped_after_the_last_identity_check_cannot_redirect_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overwrite: bool
) -> None:
    safe = tmp_path / "safe"
    safe.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if overwrite:
        (safe / "out.md").write_text("old", encoding="utf-8")
    target = llm_command.check_output_path(
        safe / "out.md", overwrite=overwrite, allow_git_worktree=True
    )
    original_check = llm_command._check_identity

    def check_then_swap(*args: object) -> None:
        original_check(*args)  # type: ignore[arg-type]
        safe.rename(tmp_path / "moved")
        safe.symlink_to(elsewhere, target_is_directory=True)

    monkeypatch.setattr(llm_command, "_check_identity", check_then_swap)
    llm_command.write_private_file(target, "content", overwrite=overwrite)
    assert not (elsewhere / "out.md").exists()
    assert list(elsewhere.iterdir()) == []
    assert (tmp_path / "moved" / "out.md").read_text(encoding="utf-8") == "content"


@pytest.mark.parametrize("separator", ZERO_WIDTH)
def test_m4_01_zero_width_separators(separator: str) -> None:
    whole = through_context(f"48{separator}99999-1111")
    single = through_context(separator.join("48999991111"))
    for body in (whole, single):
        assert digits_left(body) == "", ascii(body)


def test_m4_01_zero_width_characters_do_not_hide_names() -> None:
    body = through_context("oi Jo\u200bao Sil\u2060va", sender="Joao Silva")
    assert "Jo" not in body and "va" not in body.replace("[", ""), ascii(body)
