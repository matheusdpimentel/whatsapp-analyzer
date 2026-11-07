"""Records produced by the format adapters."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from whatsapp_analyzer.schema import Direction, MediaKind, MessageType


@dataclass(frozen=True)
class Message:
    """One message in the canonical schema. `line` is the 1-based source line or row number."""

    conversation_id: str
    timestamp: datetime
    sender: str
    direction: Direction | None
    text: str
    message_type: MessageType
    media_kind: MediaKind | None
    line: int

    def key(self) -> tuple[object, ...]:
        """Fields that identify a duplicate: everything except the source position."""
        return (
            self.conversation_id,
            self.timestamp,
            self.sender,
            self.direction,
            self.text,
            self.message_type,
            self.media_kind,
        )


class IssueCode(StrEnum):
    """Record-level findings. File-level failures are `InputError` codes."""

    # The record is excluded.
    MALFORMED_RECORD = "MALFORMED_RECORD"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_DIRECTION = "INVALID_DIRECTION"
    INVALID_VALUE = "INVALID_VALUE"
    # The record is kept (or, for duplicates, removed unless requested otherwise).
    DUPLICATE_RECORD = "DUPLICATE_RECORD"
    TIMESTAMP_OUT_OF_ORDER = "TIMESTAMP_OUT_OF_ORDER"
    CONTROL_CHARACTERS_REMOVED = "CONTROL_CHARACTERS_REMOVED"
    EMPTY_MESSAGE = "EMPTY_MESSAGE"
    TITLE_CHANGED = "TITLE_CHANGED"
    REPLY_QUOTE_REMOVED = "REPLY_QUOTE_REMOVED"
    REACTIONS_REMOVED = "REACTIONS_REMOVED"
    EMPTY_ROW = "EMPTY_ROW"
    OWNER_NOT_IN_FILE = "OWNER_NOT_IN_FILE"


class Severity(StrEnum):
    ERROR = "error"  # record excluded
    WARNING = "warning"
    INFO = "info"


SEVERITY = {
    IssueCode.MALFORMED_RECORD: Severity.ERROR,
    IssueCode.INVALID_TIMESTAMP: Severity.ERROR,
    IssueCode.MISSING_REQUIRED_FIELD: Severity.ERROR,
    IssueCode.INVALID_DIRECTION: Severity.ERROR,
    IssueCode.INVALID_VALUE: Severity.ERROR,
    IssueCode.DUPLICATE_RECORD: Severity.WARNING,
    IssueCode.TIMESTAMP_OUT_OF_ORDER: Severity.WARNING,
    IssueCode.CONTROL_CHARACTERS_REMOVED: Severity.WARNING,
    IssueCode.EMPTY_MESSAGE: Severity.WARNING,
    IssueCode.TITLE_CHANGED: Severity.WARNING,
    IssueCode.REPLY_QUOTE_REMOVED: Severity.INFO,
    IssueCode.REACTIONS_REMOVED: Severity.INFO,
    IssueCode.EMPTY_ROW: Severity.INFO,
    IssueCode.OWNER_NOT_IN_FILE: Severity.WARNING,
}


@dataclass(frozen=True)
class RecordIssue:
    """A finding about one record (`line` 0: the whole file). Never carries message content."""

    code: IssueCode
    line: int
    field: str | None = None


class InputFormat(StrEnum):
    IMAZING_TXT = "imazing-txt"
    WHATSAPP_TXT = "whatsapp-txt"
    CSV = "csv"
    XLSX = "xlsx"


class InputErrorCode(StrEnum):
    """Reasons a whole file is rejected."""

    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    UNSUPPORTED_ENCODING = "UNSUPPORTED_ENCODING"
    EMPTY_FILE = "EMPTY_FILE"
    HEADER_NOT_FOUND = "HEADER_NOT_FOUND"
    INVALID_COLUMN_MAPPING = "INVALID_COLUMN_MAPPING"
    MISSING_REQUIRED_COLUMNS = "MISSING_REQUIRED_COLUMNS"
    SHEET_NOT_FOUND = "SHEET_NOT_FOUND"
    AMBIGUOUS_SHEET = "AMBIGUOUS_SHEET"
    SPREADSHEET_CORRUPTED = "SPREADSHEET_CORRUPTED"
    AMBIGUOUS_DATE_ORDER = "AMBIGUOUS_DATE_ORDER"
    MIXED_DATE_FORMATS = "MIXED_DATE_FORMATS"
    OWNER_REQUIRED = "OWNER_REQUIRED"
    NO_VALID_RECORDS = "NO_VALID_RECORDS"
    CONVERSATION_ID_CONFLICT = "CONVERSATION_ID_CONFLICT"


class InputError(Exception):
    """A whole input file cannot be imported. `details` never contains message content."""

    def __init__(self, code: InputErrorCode, **details: object) -> None:
        super().__init__(code.value)
        self.code = code
        self.details = details
