"""Run the adapters over a set of input files and validate what they produce.

Adapters only parse. Checks that apply to every format live here: control-character clean-up,
duplicates, out-of-order timestamps and per-file statistics. Accepted messages are streamed to
`sink` as they are read; a caller that must not keep output from a rejected file (the normalized
CSV export) discards everything when the report has rejections.
"""

import hashlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from whatsapp_analyzer.ingest import imazing_txt, tabular, whatsapp_txt
from whatsapp_analyzer.ingest.detect import collect_inputs, detect_format
from whatsapp_analyzer.ingest.mapping import ColumnMapping
from whatsapp_analyzer.ingest.model import (
    SEVERITY,
    InputError,
    InputErrorCode,
    InputFormat,
    IssueCode,
    Message,
    RecordIssue,
    Severity,
)
from whatsapp_analyzer.ingest.report import FileReport, ImportReport
from whatsapp_analyzer.ingest.text_io import detect_encoding, remove_control_characters
from whatsapp_analyzer.ingest.whatsapp_txt import DateOrder

DEFAULT_OWNER_LABEL = "owner"
PROGRESS_EVERY = 25_000

Sink = Callable[[Message], None]
Progress = Callable[[int, int, int], None]  # file index, file count, records read in that file
FileStart = Callable[[FileReport, Path], None]  # called once the file's format is known


@dataclass(frozen=True)
class ImportOptions:
    format: InputFormat | None = None
    owner: str | None = None
    date_order: DateOrder | None = None
    mapping: ColumnMapping | None = None
    column_overrides: dict[str, str] = field(default_factory=dict)
    sheet: str | None = None
    header_row: int | None = None
    encoding: str | None = None
    delimiter: str | None = None
    drop_duplicates: bool = False

    @property
    def owner_label(self) -> str:
        return self.owner or DEFAULT_OWNER_LABEL

    def tabular(self) -> tabular.TabularSettings:
        return tabular.TabularSettings(
            owner_label=self.owner_label,
            mapping=self.mapping,
            overrides=self.column_overrides,
            encoding=self.encoding,
            delimiter=self.delimiter,
            sheet=self.sheet,
            header_row=self.header_row,
        )


def run_import(
    paths: list[Path],
    options: ImportOptions,
    sink: Sink | None = None,
    progress: Progress | None = None,
    on_file: FileStart | None = None,
) -> ImportReport:
    files = collect_inputs(paths, options.format)
    report = ImportReport(duplicates_removed=options.drop_duplicates)
    validator = _Validator(options, sink, progress)
    for index, path in enumerate(files, start=1):
        file_report = FileReport(index=index, name=str(path))
        report.files.append(file_report)
        try:
            _import_file(path, file_report, options, validator, (index, len(files)), on_file)
        except InputError as error:
            file_report.error = error.code
            file_report.error_details = error.details
    return report


def _import_file(
    path: Path,
    report: FileReport,
    options: ImportOptions,
    validator: "_Validator",
    position: tuple[int, int],
    on_file: FileStart | None,
) -> None:
    report.format = options.format or detect_format(path, options.encoding)
    if report.format in (InputFormat.IMAZING_TXT, InputFormat.WHATSAPP_TXT):
        validator.claim_conversation_id(path.stem, report.index)
    if on_file:
        on_file(report, path)
    validator.consume(_items(path, report.format, options), report, position)
    if report.records_valid == 0:
        raise InputError(InputErrorCode.NO_VALID_RECORDS)


def _items(
    path: Path, input_format: InputFormat, options: ImportOptions
) -> Iterator[Message | RecordIssue]:
    if input_format is InputFormat.IMAZING_TXT:
        encoding = detect_encoding(path, options.encoding)
        return imazing_txt.parse(path, encoding, options.owner_label)
    if input_format is InputFormat.WHATSAPP_TXT:
        encoding = detect_encoding(path, options.encoding)
        return whatsapp_txt.parse(path, encoding, options.owner, options.date_order)
    if input_format is InputFormat.CSV:
        return tabular.parse_csv(path, options.tabular())
    return tabular.parse_xlsx(path, options.tabular())


class _Validator:
    """Checks shared by all formats. Duplicates are detected across the whole import.

    Duplicates are kept unless `drop_duplicates` is set: exports with minute precision (Android)
    make two genuine "ok" messages in the same minute identical, so removal is opt-in.
    """

    def __init__(
        self, options: ImportOptions, sink: Sink | None, progress: Progress | None
    ) -> None:
        self._drop_duplicates = options.drop_duplicates
        self._sink = sink
        self._progress = progress
        self._seen: set[bytes] = set()
        self._txt_conversations: dict[str, int] = {}

    def claim_conversation_id(self, conversation_id: str, file_index: int) -> None:
        """TXT files are one chat each, named after the file; two files with the same name
        (such as iOS `_chat.txt` exports) would silently merge into one conversation."""
        owner = self._txt_conversations.setdefault(conversation_id, file_index)
        if owner != file_index:
            raise InputError(InputErrorCode.CONVERSATION_ID_CONFLICT, same_name_as_file=owner)

    def consume(
        self, items: Iterable[Message | RecordIssue], report: FileReport, position: tuple[int, int]
    ) -> None:
        last_timestamp: dict[str, datetime] = {}
        if self._progress:
            self._progress(*position, 0)
        for item in items:
            if isinstance(item, RecordIssue):
                report.add_issue(item)
                # Adapters emit exactly one error-severity issue for each record they reject.
                if SEVERITY[item.code] is Severity.ERROR:
                    report.records_read += 1
                continue
            report.records_read += 1
            message = self._clean(item, report)
            report.records_valid += 1
            report.conversations.add(message.conversation_id)
            report.add_timestamp(message.timestamp)

            previous = last_timestamp.get(message.conversation_id)
            if previous is not None and message.timestamp < previous:
                report.add_issue(RecordIssue(IssueCode.TIMESTAMP_OUT_OF_ORDER, message.line))
            else:
                last_timestamp[message.conversation_id] = message.timestamp

            if self._is_duplicate(message):
                report.duplicates += 1
                report.add_issue(RecordIssue(IssueCode.DUPLICATE_RECORD, message.line))
                if self._drop_duplicates:
                    continue
            if self._sink:
                self._sink(message)
            if self._progress and report.records_read % PROGRESS_EVERY == 0:
                self._progress(*position, report.records_read)

    def _is_duplicate(self, message: Message) -> bool:
        # A 16-byte digest per message keeps memory bounded for large imports.
        digest = hashlib.blake2b(repr(message.key()).encode(), digest_size=16).digest()
        if digest in self._seen:
            return True
        self._seen.add(digest)
        return False

    @staticmethod
    def _clean(message: Message, report: FileReport) -> Message:
        text, removed_from_text = remove_control_characters(message.text)
        sender, removed_from_sender = remove_control_characters(message.sender)
        if not (removed_from_text or removed_from_sender):
            return message
        report.add_issue(RecordIssue(IssueCode.CONTROL_CHARACTERS_REMOVED, message.line))
        return replace(message, text=text, sender=sender)
