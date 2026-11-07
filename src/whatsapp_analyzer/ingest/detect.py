"""Input format detection, kept separate from parsing and validation."""

from itertools import islice
from pathlib import Path

from whatsapp_analyzer.ingest import imazing_txt, whatsapp_txt
from whatsapp_analyzer.ingest.model import InputError, InputErrorCode, InputFormat
from whatsapp_analyzer.ingest.text_io import detect_encoding, iter_lines, open_text

SUPPORTED_SUFFIXES = {".txt", ".csv", ".xlsx"}
FORMAT_SUFFIXES = {
    InputFormat.IMAZING_TXT: {".txt"},
    InputFormat.WHATSAPP_TXT: {".txt"},
    InputFormat.CSV: {".csv"},
    InputFormat.XLSX: {".xlsx"},
}
PROBE_LINES = 40


def detect_format(path: Path, encoding: str | None) -> InputFormat:
    if path.is_dir():
        raise InputError(InputErrorCode.UNSUPPORTED_FORMAT)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return InputFormat.CSV
    if suffix == ".xlsx":
        return InputFormat.XLSX
    if suffix != ".txt":
        raise InputError(InputErrorCode.UNSUPPORTED_FORMAT)

    with open_text(path, detect_encoding(path, encoding)) as handle:
        first_lines = list(islice(iter_lines(handle), PROBE_LINES))
    if not any(line.strip() for line in first_lines):
        raise InputError(InputErrorCode.EMPTY_FILE)
    if imazing_txt.looks_like(first_lines):
        return InputFormat.IMAZING_TXT
    if whatsapp_txt.looks_like(first_lines):
        return InputFormat.WHATSAPP_TXT
    raise InputError(InputErrorCode.UNSUPPORTED_FORMAT)


def collect_inputs(paths: list[Path], forced: InputFormat | None = None) -> list[Path]:
    """Expand directories (not recursively) into their supported files, in name order.

    With a forced format, directories contribute only files with that format's extension.
    A file reached twice (for example listed and inside a listed directory) is kept once.
    Paths that do not exist are kept, so they are reported as `FILE_NOT_FOUND`.
    """
    suffixes = FORMAT_SUFFIXES[forced] if forced else SUPPORTED_SUFFIXES
    files: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path.is_dir():
            candidates = sorted(
                child
                for child in path.iterdir()
                if child.is_file()
                and child.suffix.lower() in suffixes
                and not child.name.startswith(".")
            )
        else:
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(candidate)
    return files
