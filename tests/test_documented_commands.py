"""Documentation must match the CLI: every documented command and option exists.

`scripts/check_quickstart.py` runs the whole workflow; this module checks, cheaply and
offline, that each `whatsapp-analyzer ...` line in the README and docs names a real command
path and only real options, so a renamed command or flag fails the suite.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from whatsapp_analyzer.cli.app import main

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]
LINE = re.compile(r"^\s*(?:\$ )?(?:uv run )?whatsapp-analyzer\s+(?P<rest>[^#`|]+)")
OPTION = re.compile(r"^--[a-z][a-z0-9-]*$")
WORD = re.compile(r"^[a-z][a-z-]*$")
GLOBAL_OPTIONS = {"--json", "--lang", "--version", "--help"}
# Words that follow a command as ordinary arguments, never as subcommands.
MAX_DEPTH = 3


def _help(capsys: pytest.CaptureFixture[str], *words: str) -> str | None:
    capsys.readouterr()
    try:
        code = main([*words, "--help"])
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    out = capsys.readouterr().out
    return out if code == 0 else None


def _documented_lines() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in DOCS:
        in_fence = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            match = LINE.match(raw)
            if match:
                found.append((path.name, match["rest"].strip()))
    return found


def test_documented_commands_and_options_exist(capsys: pytest.CaptureFixture[str]) -> None:
    lines = _documented_lines()
    assert len(lines) > 30, "documentation examples were not found"
    cache: dict[tuple[str, ...], str | None] = {}
    problems: list[str] = []
    for doc, rest in lines:
        tokens = rest.split()
        while tokens and tokens[0] in {"--json"} | {"--lang"}:
            tokens = tokens[2:] if tokens[0] == "--lang" else tokens[1:]
        path: list[str] = []
        for token in tokens[:MAX_DEPTH]:
            if not WORD.match(token):
                break
            candidate = (*path, token)
            if candidate not in cache:
                cache[candidate] = _help(capsys, *candidate)
            if cache[candidate] is None:
                break
            path.append(token)
        if not path:
            if tokens and not tokens[0].startswith("-"):
                problems.append(f"{doc}: unknown command in 'whatsapp-analyzer {rest}'")
            continue
        text = cache[tuple(path)] or ""
        for token in tokens:
            option = token.split("=", 1)[0]
            if OPTION.match(option) and option not in GLOBAL_OPTIONS and option not in text:
                problems.append(f"{doc}: '{option}' is not an option of '{' '.join(path)}'")
    assert not problems, "\n".join(sorted(set(problems)))


def test_quickstart_script_runs_on_the_development_install() -> None:
    """The full documented workflow (synthetic data, fake provider, no network)."""
    scripts = Path(sys.executable).parent
    exe = scripts / ("whatsapp-analyzer.exe" if sys.platform == "win32" else "whatsapp-analyzer")
    if not exe.exists():
        pytest.skip("console script not installed next to the interpreter")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_quickstart.py"), str(exe)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "quickstart check passed" in proc.stdout


def test_readme_pre_release_install_commands_are_valid() -> None:
    """`pipx install --pre` is not valid pipx: pre-releases go through --pip-args."""
    docs = "\n".join(p.read_text(encoding="utf-8") for p in DOCS)
    assert not re.search(r"pipx install\s+--pre\b", docs)
    assert "pipx install --pip-args='--pre' whatsapp-conversation-analyzer" in docs
    # uvx must name the package with --from: the command differs from the package name.
    assert not re.search(r"uvx\s+whatsapp-conversation-analyzer\b", docs)
    assert (
        "uvx --from 'whatsapp-conversation-analyzer==1.0.0b1' whatsapp-analyzer --version" in docs
    )
