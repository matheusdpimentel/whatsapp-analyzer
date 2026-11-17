"""Guard against real or personal data entering the repository or the package.

Synthetic sentinels (fake keys in tests, 555-01XX phones, example.* addresses) are allowed
on purpose and recognized by explicit markers; anything else that looks real fails.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELF = "tests/test_repository_hygiene.py"

FORBIDDEN_FILES = re.compile(r"(\.db(-journal|-wal|-shm)?|\.pem|\.key|\.log|\.pyc)$|(^|/)\.env$")
PERSONAL = re.compile(r"/Users/[A-Za-z]|/home/[a-z]+/|C:\\Users\\|matheuspimentel", re.I)
SECRET = re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}")
SENTINEL = re.compile(r"test|fake|sentinel|example|dummy|not-a-real|ABCDEFGH|0123456789", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
FAKE_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
PHONE_LIKE = re.compile(r"(?<![\d:.-])(?:\(?\d{2,3}\)?[ -]?)?\d{3,5}[ -]\d{4}(?![\d:.-])")


def _tracked() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [p for p in out.split("\0") if p and p != "uv.lock"]


def _text(path: str) -> str | None:
    try:
        return (ROOT / path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return None


def test_no_databases_keys_logs_or_bytecode_are_tracked() -> None:
    bad = [p for p in _tracked() if FORBIDDEN_FILES.search(p)]
    assert not bad, bad


def test_no_personal_paths_or_real_looking_secrets() -> None:
    problems: list[str] = []
    for path in _tracked():
        if path == SELF:
            continue
        text = _text(path)
        if text is None:
            continue
        for match in PERSONAL.finditer(text):
            problems.append(f"{path}: personal path or name {match.group(0)!r}")
        for match in SECRET.finditer(text):
            if not SENTINEL.search(match.group(0)):
                problems.append(f"{path}: real-looking secret {match.group(0)[:8]}...")
    assert not problems, "\n".join(problems)


def test_only_fictional_email_addresses() -> None:
    allowed_in_tests = ("gmail.com", "hotmail.com", "outlook.com")  # masking inputs
    problems = []
    for path in _tracked():
        text = _text(path)
        if text is None or path == SELF:
            continue
        for match in EMAIL.finditer(text):
            domain = match.group(1).lower()
            if domain.endswith(FAKE_DOMAINS):
                continue
            if path.startswith("tests/") and domain in allowed_in_tests:
                continue
            problems.append(f"{path}: {match.group(0)}")
    assert not problems, "\n".join(problems)


def test_example_phone_numbers_are_the_reserved_555_01xx_range() -> None:
    problems = []
    seen = 0
    for path in _tracked():
        if not path.startswith("examples/") or not path.endswith((".txt", ".csv")):
            continue
        for match in PHONE_LIKE.finditer(_text(path) or ""):
            seen += 1
            if not re.search(r"555[- ]01\d\d$", match.group(0)):
                problems.append(f"{path}: {match.group(0)}")
    assert seen > 0, "no example phone numbers found: the pattern no longer matches"
    assert not problems, "\n".join(sorted(set(problems))[:10])
