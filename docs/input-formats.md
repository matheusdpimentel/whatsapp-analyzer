# Input formats, validation and normalized CSV

`whatsapp-analyzer import` reads conversation exports, validates them and converts every
message into the canonical schema. It can also write the result as a normalized CSV file.

```bash
whatsapp-analyzer import ./exports                               # validate, print a report
whatsapp-analyzer import ./exports --normalized-out messages.csv  # also write canonical CSV
whatsapp-analyzer --json import ./exports                        # report as JSON (for scripts/agents)
```

Directories are read non-recursively; files are processed in name order, and a file reached
twice is read once. With `--format`, directories contribute only files with that format's
extension. Global options such as `--json` and `--lang` go before `import`.

## Supported formats

| Format | `--format` | Detected by | Status |
|--------|-----------|-------------|--------|
| iMazing chat TXT | `imazing-txt` | `.txt` starting with a 52-dash separator and an ISO timestamp header | Validated against real exports |
| iMazing CSV | `csv` (preset `imazing-csv`) | `.csv` with the iMazing column names | Validated against real exports |
| Canonical CSV | `csv` (preset `canonical`) | `.csv` with the canonical columns | Round-trip tested |
| Other CSV / XLSX | `csv`, `xlsx` | Needs a column mapping | Tested with synthetic files |
| WhatsApp "Export chat" TXT | `whatsapp-txt` | `.txt` with `d/m/y, h:mm - Sender:` or `[d/m/y, h:mm:ss] Sender:` lines | **Experimental**: modeled on common layouts, not validated against real exports |

"Validated" means the adapter was checked locally against real iMazing exports in 2026, using
scripts that only looked at structure. Other iMazing versions or settings may differ; please
report structural differences (never the content) in an issue.

## Canonical schema

| Column | Required | Values |
|--------|----------|--------|
| `conversation_id` | yes | Any non-empty text. TXT inputs use the file name without extension. |
| `timestamp` | yes | ISO 8601 local time without offset, e.g. `2026-01-31T14:05:09`. |
| `sender` | no | Display name. Sent messages without a name get the `--owner` value or `owner`. |
| `direction` | yes, except system messages | `inbound`, `outbound` (empty for system messages) |
| `text` | no | Message text or media caption. Multiline text is kept. |
| `message_type` | no | `text`, `media`, `deleted`, `system`. Derived when not mapped. |
| `media_kind` | no | `audio`, `image`, `video`, `sticker`, `document`, `contact`, `location` |

Reply quotes, reactions, delivery status and forwarding flags are not part of the schema.

## iMazing TXT

One file per chat. Each record starts with a line of exactly 52 dashes, followed by the chat
title and a header such as `2024-03-01 10:00:00 from Alex Rivera (+1 555 0100)` (received) or
`2024-03-01 10:00:00 to <chat title>` (sent).

- A dash line only starts a record when a title and a valid header follow it, so dashes typed
  inside a message stay in that message.
- The phone number in parentheses is removed from the sender name.
- Attachment lines (`name.opus (Audio)`, `(Image)`, `(Sticker)`, `(Video)`, `(Gif)`) and the
  duration, size or page-count lines that follow them are not text. A document is a file name
  followed by an `N pages ·` line; a file name followed only by a size stays text, because a
  typed "see report.pdf" + "2 MB" looks the same. Remaining lines are the caption.
- A leading reply quote (`↩ ..., 2024-02-28 09:00:00: ...` up to the next blank line) and a
  trailing block of reactions (after a blank line, every line shaped `<who> <emoji> <word>
  dd/mm/yy, hh:mm`, where the emoji token must contain a pictographic character) are removed and counted as `REPLY_QUOTE_REMOVED` and `REACTIONS_REMOVED`.
  A quote without a following blank line is left in the text, because its end is unknown.
  The quoted text and the reactions themselves are **not** kept in the normalized output; only
  their counts remain in the report. Optional preservation is a planned improvement.
- `This message was deleted` / `You deleted this message` become `deleted` messages.

Limitations: shared contacts and locations appear as plain text in these files and cannot be
told apart from text; some media records have no attachment line and import as text. The
iMazing CSV keeps both kinds.

**How closely TXT and CSV agree.** In a local check of 91 chats exported both ways (about
152,000 messages matched by chat, timestamp and direction), the imported message text was
identical in about 95.2% of the matched messages, and message type and media kind in about
98.7%. This is **not** an equivalence: the two exports of the same chats differ, and the
remaining differences are only partly explained (contacts and locations shown as text, media
records without an attachment line, quotes without a closing blank line, different sender
names). They still need to be characterized; prefer the CSV when both are available.

## iMazing CSV

Recognized automatically by its columns. `Chat Session` becomes `conversation_id`,
`Message Date` the timestamp, `Type` the direction (`Incoming`, `Outgoing`, `Notification` =
system message), `Sender Name` the sender, `Text` the text and `Attachment type` the media kind
(`Attachment` = document, `Gif` = video). `Sent Date`, `Sender ID`, `Status`, `Forwarded`,
`Replying to`, `Reactions`, `Attachment` and `Attachment info` are not imported.

In the same export, sender names can differ from the TXT files (for example, received messages
in one-to-one chats often have an empty `Sender Name`).

## WhatsApp "Export chat" TXT (experimental)

- `--owner NAME` is required: native exports do not mark which messages you sent. If the name
  never appears in a file, every message counts as received and the report shows
  `OWNER_NOT_IN_FILE`.
- Day/month order is inferred from the whole file. When every day is 12 or lower the file is
  rejected with `AMBIGUOUS_DATE_ORDER`; pass `--date-order dmy`, `mdy` or `ymd`. Files that mix
  both orders are rejected with `MIXED_DATE_FORMATS`.
- Android exports have no seconds and say `<Media omitted>` without the media kind. Whether they
  keep captions has not been verified; captions on the lines after the marker are imported.
- Android system lines that contain `": "` (for example a group subject with a colon) cannot be
  told apart from messages and are imported as messages.
- iOS exports are usually all named `_chat.txt`. Each TXT file is one conversation named after
  the file, so give the files distinct names; same-named files in one import are rejected with
  `CONVERSATION_ID_CONFLICT` instead of being merged.

## Other CSV and XLSX files: column mapping

Column names are never guessed. Without a recognized preset, the import stops with
`MISSING_REQUIRED_COLUMNS` and lists the unmapped fields. Provide a mapping:

```bash
whatsapp-analyzer import data.csv --mapping my-mapping.toml
whatsapp-analyzer import data.csv --map conversation_id=chat --map timestamp=sent_at --map direction=dir
whatsapp-analyzer import data.csv --mapping imazing-csv      # force a preset
```

Mapping file (see `examples/mappings/custom-columns.toml`):

```toml
[columns]            # canonical field = source column
conversation_id = "chat_id"
timestamp = "sent_at"
direction = "from_me"
text = "body"

[timestamp]
format = "%d/%m/%Y %H:%M:%S"   # Python strptime format; ISO 8601 when omitted

[values.direction]   # source value = canonical value ("system" marks system messages)
"true" = "outbound"
"false" = "inbound"
```

`--map` options override the mapping file or preset. A mapped column that is missing from the
file, or that appears twice in the header, rejects the file with `INVALID_COLUMN_MAPPING`.

**CSV**: UTF-8 with or without BOM, or UTF-16 with BOM. Other encodings are not guessed; pass
`--encoding cp1252` (for example) when the report says `UNSUPPORTED_ENCODING`. The delimiter
(`,` `;` tab `|`) is detected from the header line or set with `--delimiter`.

**XLSX**: read in read-only mode; cell formulas are never evaluated (cached values are used).
With several worksheets, the one whose header fits a mapping is used, or pass `--sheet`. The
header row is searched in the first 20 rows, or set with `--header-row`. Date cells are read as
timestamps; text cells are parsed like CSV values.

Timestamps with a UTC offset are rejected (`INVALID_TIMESTAMP`) rather than converted.

## Validation report

Every import prints a summary (files inspected, accepted and rejected; records read, valid and
invalid; duplicates; conversations; period; formats) and a table of findings. With `--json` the
same data is in `data.totals`, `data.issues`, `data.rejections` and `data.files`.

Reports never contain message text or sender names. Files are identified by position
(`file #3`); add `--show-file-names` to include names, which often contain contact names.

Record findings:

| Code | Severity | Effect |
|------|----------|--------|
| `MALFORMED_RECORD`, `INVALID_TIMESTAMP`, `MISSING_REQUIRED_FIELD`, `INVALID_DIRECTION`, `INVALID_VALUE` | error | Record skipped |
| `DUPLICATE_RECORD` | warning | Kept; removed with `--drop-duplicates` |
| `TIMESTAMP_OUT_OF_ORDER` | warning | Kept in source order |
| `CONTROL_CHARACTERS_REMOVED` | warning | C0 control characters removed (emoji joiners and direction marks are kept) |
| `EMPTY_MESSAGE`, `TITLE_CHANGED`, `OWNER_NOT_IN_FILE` | warning | Kept |
| `REPLY_QUOTE_REMOVED`, `REACTIONS_REMOVED`, `EMPTY_ROW` | info | See above |

Duplicates are exact repeats of every canonical field. They are kept by default because exports
without seconds make genuine repeated messages ("ok" twice in a minute) identical.

File rejections: `FILE_NOT_FOUND`, `PERMISSION_DENIED`, `UNSUPPORTED_FORMAT`,
`UNSUPPORTED_ENCODING`, `EMPTY_FILE`, `HEADER_NOT_FOUND`, `INVALID_COLUMN_MAPPING`,
`MISSING_REQUIRED_COLUMNS`, `SHEET_NOT_FOUND`, `AMBIGUOUS_SHEET`, `SPREADSHEET_CORRUPTED`,
`AMBIGUOUS_DATE_ORDER`, `MIXED_DATE_FORMATS`, `OWNER_REQUIRED`, `NO_VALID_RECORDS`,
`CONVERSATION_ID_CONFLICT`. Rejected files still list their record findings and line numbers in
the JSON report.

Exit codes: `0` when every file is accepted (record findings do not fail the import unless
`--strict` is given), `3` when a file is rejected or `--strict` finds invalid records, `2` for
invalid options (including an existing output without `--overwrite`, or no input files), `11`
when the output cannot be written or moved into place. When one rejection code covers all rejected files, it is the JSON
`error.code`; otherwise `VALIDATION_FAILED`.

## Normalized CSV

`--normalized-out FILE` writes accepted messages in canonical column order, UTF-8 without BOM,
standard CSV quoting (multiline text preserved), ISO 8601 timestamps and source order.

- It is written to a temporary file and moved into place only when every input file is
  accepted. A rejected or interrupted import leaves no partial file.
- An existing file is only replaced with `--overwrite`.
- On macOS and Linux the file is readable only by you (mode 600), including when it replaces an
  existing file, because it contains private messages.
- **Spreadsheet formula protection**: values in `conversation_id`, `sender` and `text` that
  start with `=`, `+`, `-`, `@`, tab or carriage return get a leading `'`, which spreadsheet
  applications show as plain text instead of executing. Values that already start with `'`
  followed by one of those characters (or another `'`) are escaped the same way, and importing
  the file with the `canonical` mapping removes exactly one escaping `'`, so the export
  round-trips without changes. Other mappings keep leading apostrophes as data.

The normalized file contains your messages; keep it as private as the original exports.

## Performance

Files are streamed: memory does not grow with the number of messages kept, only with the
duplicate index (16 bytes per message plus set overhead) and per-conversation bookkeeping.
Measured on an Apple M4 laptop, macOS 15.7, Python 3.11, with synthetic data:

| Input | Records | Time | Peak memory (RSS) |
|-------|---------|------|-------------------|
| Canonical CSV, 89 MB, 20,000 conversations, written back as normalized CSV | 1,000,000 | 7.9 s | 156 MB |
| 3,002 synthetic Android TXT files | 83,661 | 0.8 s | 53 MB |

These are single runs on one machine, not guarantees.

Known reporting limits: a CSV field with an unterminated quote swallows the rows after it and is
reported as one `MALFORMED_RECORD`; duplicates are counted against messages from every file read
so far, including files that end up rejected.

## Troubleshooting

| Report says | Try |
|-------------|-----|
| `UNSUPPORTED_FORMAT` for a `.txt` | The file is neither an iMazing nor a WhatsApp export; check its first lines. |
| `UNSUPPORTED_ENCODING` | `--encoding cp1252` (or the encoding your tool used). |
| `MISSING_REQUIRED_COLUMNS` | Add `--mapping` or `--map FIELD=COLUMN`. |
| `HEADER_NOT_FOUND` (XLSX) | `--header-row N` and, with several sheets, `--sheet NAME`. |
| `AMBIGUOUS_DATE_ORDER` | `--date-order dmy` (most countries) or `mdy` (US). |
| Many `INVALID_TIMESTAMP` in a CSV | Add `[timestamp] format = "..."` to the mapping file. |
| `OWNER_NOT_IN_FILE` on every file | `--owner` must match your name exactly as it appears in the export. |

## Privacy

Imports run locally and send nothing anywhere. Reports and logs contain counts, codes and
line numbers, never message text. Real exports must never be added to this repository; all
examples and tests use synthetic data from `whatsapp-analyzer demo-data`.

## For exporter authors

To produce files this tool reads without a mapping, write the canonical CSV described above
(header row with the seven column names, UTF-8, ISO 8601 timestamps without offset).
