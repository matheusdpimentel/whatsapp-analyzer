"""Text decoding, line iteration and text clean-up shared by the adapters."""

import codecs
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO

from whatsapp_analyzer.ingest.model import InputError, InputErrorCode

# C0 control characters other than tab and line breaks break CSV tools and terminals. Format
# characters such as LRM or the zero-width joiner (used by emoji) are kept.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def detect_encoding(path: Path, requested: str | None) -> str:
    """Return the encoding to read `path` with.

    Without an explicit choice, UTF-16 is used only when a UTF-16 byte order mark is present and
    everything else is read as UTF-8 (with or without BOM). Other encodings are never guessed: the
    user passes `--encoding` after the import reports `UNSUPPORTED_ENCODING`.
    """
    if requested:
        try:
            codecs.lookup(requested)
        except LookupError as exc:
            raise InputError(InputErrorCode.UNSUPPORTED_ENCODING, encoding=requested) from exc
        return requested
    with _open_binary(path) as handle:
        head = handle.read(4)
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    return "utf-8-sig"


@contextmanager
def open_text(path: Path, encoding: str) -> Iterator[TextIO]:
    """Open for reading with strict decoding; decoding errors become `UNSUPPORTED_ENCODING`."""
    try:
        with (
            _translate_os_errors(path),
            path.open(encoding=encoding, errors="strict", newline="") as handle,
        ):
            yield handle
    except UnicodeDecodeError as exc:
        raise InputError(InputErrorCode.UNSUPPORTED_ENCODING, encoding=encoding) from exc


def iter_lines(handle: TextIO) -> Iterator[str]:
    """Yield lines without their line terminator, accepting LF, CRLF and mixed files."""
    for line in handle:
        yield line.rstrip("\r\n")


@contextmanager
def _open_binary(path: Path) -> Iterator[BinaryIO]:
    with _translate_os_errors(path), path.open("rb") as handle:
        yield handle


@contextmanager
def _translate_os_errors(path: Path) -> Iterator[None]:
    try:
        yield
    except FileNotFoundError as exc:
        raise InputError(InputErrorCode.FILE_NOT_FOUND) from exc
    except PermissionError as exc:
        raise InputError(InputErrorCode.PERMISSION_DENIED) from exc
    except IsADirectoryError as exc:
        raise InputError(InputErrorCode.UNSUPPORTED_FORMAT) from exc


def remove_control_characters(text: str) -> tuple[str, int]:
    cleaned, count = _CONTROL_CHARACTERS.subn("", text)
    return cleaned, count


# Exporter placeholders for deleted messages, in the languages seen so far.
DELETED_MESSAGE_TEXTS = frozenset(
    {
        "This message was deleted",
        "This message was deleted.",
        "You deleted this message",
        "You deleted this message.",
        "Mensagem apagada",
        "Esta mensagem foi apagada",
        "Você apagou esta mensagem",
    }
)
