"""Atomic file output shared by the exporters."""

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


def atomic_write_files(*outputs: tuple[Path, Iterable[str]]) -> None:
    """Write every file to a temporary file next to it first, then move them all into place.

    A failure while writing (the likely case: disk full, permissions) leaves the previous files
    untouched and never a partial file. If a rename itself fails, files written before it stay
    in place; every CSV row repeats the run id and status, so a mismatch is detectable."""
    temporaries: list[tuple[Path, Path]] = []
    try:
        for path, chunks in outputs:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporaries.append((Path(name), path))
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                for chunk in chunks:
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, path in temporaries:
            temporary.replace(path)
        _sync_directories({path.parent for _, path in temporaries})
    finally:
        for temporary, _ in temporaries:
            temporary.unlink(missing_ok=True)


def _sync_directories(directories: set[Path]) -> None:
    """Make the renames durable (POSIX); Windows cannot open directories for fsync."""
    if os.name == "nt":
        return
    for directory in directories:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
