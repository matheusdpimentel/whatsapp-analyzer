"""Validation report for an import. It holds counts and positions, never message content.

Files are identified by their position in the import (`index`); names are added only when the
user asks for them, because file names often contain contact names.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from whatsapp_analyzer.ingest.model import (
    SEVERITY,
    InputErrorCode,
    InputFormat,
    IssueCode,
    RecordIssue,
    Severity,
)

MAX_LOCATIONS_PER_CODE = 5


@dataclass
class FileReport:
    index: int
    name: str
    format: InputFormat | None = None
    error: InputErrorCode | None = None
    error_details: dict[str, object] = field(default_factory=dict)
    records_read: int = 0
    records_valid: int = 0
    records_invalid: int = 0
    duplicates: int = 0
    conversations: set[str] = field(default_factory=set)
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    issues: Counter[IssueCode] = field(default_factory=Counter)
    locations: dict[IssueCode, list[int]] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.error is None

    def add_issue(self, issue: RecordIssue) -> None:
        self.issues[issue.code] += 1
        lines = self.locations.setdefault(issue.code, [])
        if len(lines) < MAX_LOCATIONS_PER_CODE:
            lines.append(issue.line)
        if SEVERITY[issue.code] is Severity.ERROR:
            self.records_invalid += 1

    def add_timestamp(self, timestamp: datetime) -> None:
        if self.first_timestamp is None or timestamp < self.first_timestamp:
            self.first_timestamp = timestamp
        if self.last_timestamp is None or timestamp > self.last_timestamp:
            self.last_timestamp = timestamp


@dataclass
class ImportReport:
    files: list[FileReport] = field(default_factory=list)
    duplicates_removed: bool = False

    @property
    def accepted_files(self) -> list[FileReport]:
        return [f for f in self.files if f.accepted]

    @property
    def rejected_files(self) -> list[FileReport]:
        return [f for f in self.files if not f.accepted]

    def totals(self) -> dict[str, int]:
        accepted = self.accepted_files
        valid = sum(f.records_valid for f in accepted)
        duplicates = sum(f.duplicates for f in accepted)
        return {
            "files_inspected": len(self.files),
            "files_accepted": len(accepted),
            "files_rejected": len(self.files) - len(accepted),
            "records_read": sum(f.records_read for f in accepted),
            "records_valid": valid,
            "records_invalid": sum(f.records_invalid for f in accepted),
            "duplicates": duplicates,
            "records_written": valid - duplicates if self.duplicates_removed else valid,
            "conversations": len({c for f in accepted for c in f.conversations}),
        }

    def issue_counts(self) -> Counter[IssueCode]:
        total: Counter[IssueCode] = Counter()
        for report in self.accepted_files:
            total.update(report.issues)
        return total

    def date_range(self) -> tuple[datetime, datetime] | None:
        firsts = [f.first_timestamp for f in self.accepted_files if f.first_timestamp]
        lasts = [f.last_timestamp for f in self.accepted_files if f.last_timestamp]
        return (min(firsts), max(lasts)) if firsts and lasts else None

    def to_dict(self, include_file_names: bool) -> dict[str, object]:
        date_range = self.date_range()
        return {
            "totals": self.totals(),
            "date_range": (
                {"first": date_range[0].isoformat(), "last": date_range[1].isoformat()}
                if date_range
                else None
            ),
            "formats": dict(Counter(f.format.value for f in self.accepted_files if f.format)),
            "issues": {
                code.value: {"severity": SEVERITY[code].value, "count": count}
                for code, count in sorted(self.issue_counts().items())
            },
            "rejections": dict(Counter(f.error.value for f in self.rejected_files if f.error)),
            "files": [_file_dict(f, include_file_names) for f in self.files],
        }


def _file_dict(report: FileReport, include_name: bool) -> dict[str, object]:
    data: dict[str, object] = {
        "index": report.index,
        "format": report.format.value if report.format else None,
        "accepted": report.accepted,
    }
    if include_name:
        data["name"] = report.name
    if report.error:
        data["error"] = {"code": report.error.value, **report.error_details}
    data.update(
        records_read=report.records_read,
        records_valid=report.records_valid,
        records_invalid=report.records_invalid,
        duplicates=report.duplicates,
        conversations=len(report.conversations),
        first_timestamp=report.first_timestamp.isoformat() if report.first_timestamp else None,
        last_timestamp=report.last_timestamp.isoformat() if report.last_timestamp else None,
        issues={code.value: count for code, count in sorted(report.issues.items())},
        issue_lines={code.value: lines for code, lines in sorted(report.locations.items())},
    )
    return data
