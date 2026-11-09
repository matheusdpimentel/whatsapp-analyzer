"""Local environment checks used by `whatsapp-analyzer doctor`.

Checks only inspect the local machine. They never contact a provider and never read the value of
a credential, only whether the variable is set. Each check returns data plus a message key, so
rendering and translation stay in the CLI layer.
"""

import contextlib
import importlib.util
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from whatsapp_analyzer.llm.redaction import CREDENTIAL_ENV_VARS

PROVIDER_PACKAGES = ("openai", "anthropic")

_GIB = 1024**3


class Status(StrEnum):
    OK = "ok"
    FAIL = "fail"
    INFO = "info"


@dataclass(frozen=True)
class Check:
    id: str
    status: Status
    value: object
    message_key: str
    params: Mapping[str, object] = field(default_factory=dict)


def check_python(version_info: tuple[int, int, int] | None = None) -> Check:
    # The package itself requires Python 3.11+, so an unsupported interpreter never gets here.
    version = ".".join(map(str, version_info or sys.version_info[:3]))
    return Check(
        "python_version", Status.OK, version, "doctor.check.python_version.ok", {"version": version}
    )


def check_platform() -> Check:
    value = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }
    return Check("platform", Status.INFO, value, "doctor.check.platform.value", value)


def check_cpu_count(count: int | None) -> Check:
    if count is None:
        return Check("cpu_count", Status.INFO, None, "doctor.check.cpu_count.unavailable")
    return Check("cpu_count", Status.INFO, count, "doctor.check.cpu_count.value", {"count": count})


def total_physical_memory() -> int | None:
    """Total physical memory in bytes, or None where the platform does not expose it (Windows)."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None
    return total if total > 0 else None


def check_memory(total_bytes: int | None) -> Check:
    if total_bytes is None:
        return Check("memory_total", Status.INFO, None, "doctor.check.memory_total.unavailable")
    return Check(
        "memory_total",
        Status.INFO,
        total_bytes,
        "doctor.check.memory_total.value",
        {"gib": f"{total_bytes / _GIB:.1f}"},
    )


def check_disk(path: Path) -> Check:
    try:
        resolved = path.resolve(strict=True)
        free = shutil.disk_usage(resolved).free
    except OSError:  # e.g. the working directory was deleted
        return Check("disk_free", Status.INFO, None, "doctor.check.disk_free.unavailable")
    return Check(
        "disk_free",
        Status.INFO,
        {"path": str(resolved), "free_bytes": free},
        "doctor.check.disk_free.value",
        {"gib": f"{free / _GIB:.1f}", "path": str(resolved)},
    )


def sqlite_supports_wal() -> bool:
    # WAL is not available for in-memory databases, so probe a real temporary file.
    with tempfile.TemporaryDirectory() as directory:
        with contextlib.closing(sqlite3.connect(Path(directory) / "probe.db")) as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        return str(mode).lower() == "wal"


def check_sqlite(wal_supported: bool) -> Check:
    version = sqlite3.sqlite_version
    return Check(
        "sqlite",
        Status.OK if wal_supported else Status.FAIL,
        {"version": version, "wal": wal_supported},
        "doctor.check.sqlite." + ("ok" if wal_supported else "fail"),
        {"version": version},
    )


def check_provider_sdks() -> Check:
    installed = [name for name in PROVIDER_PACKAGES if importlib.util.find_spec(name) is not None]
    if not installed:
        return Check("provider_sdks", Status.INFO, [], "doctor.check.provider_sdks.none")
    return Check(
        "provider_sdks",
        Status.INFO,
        installed,
        "doctor.check.provider_sdks.value",
        {"packages": ", ".join(installed)},
    )


def check_credentials(environ: Mapping[str, str]) -> Check:
    present = {name: bool(environ.get(name)) for name in CREDENTIAL_ENV_VARS}
    names = [name for name, is_set in present.items() if is_set]
    if not names:
        return Check("credentials", Status.INFO, present, "doctor.check.credentials.none")
    return Check(
        "credentials",
        Status.INFO,
        present,
        "doctor.check.credentials.value",
        {"names": ", ".join(names)},
    )


def run_checks(path: Path, environ: Mapping[str, str]) -> list[Check]:
    return [
        check_python(),
        check_platform(),
        check_cpu_count(os.cpu_count()),
        check_memory(total_physical_memory()),
        check_disk(path),
        check_sqlite(sqlite_supports_wal()),
        check_provider_sdks(),
        check_credentials(environ),
    ]


def summarize(checks: list[Check]) -> dict[str, int]:
    return {status.value: sum(check.status is status for check in checks) for status in Status}
