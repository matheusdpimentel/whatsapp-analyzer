# ADR 0003: Import adapters and validation

- Status: Accepted
- Date: 2026-11-07

## Context

Conversations arrive as iMazing TXT and CSV exports, as WhatsApp's own TXT exports, and as
arbitrary CSV/XLSX files. The real iMazing files were inspected locally with scripts that
printed only aggregate structure (no names, numbers or text). They are not native WhatsApp
exports and must not be treated as interchangeable with them.

## Decision

- **One adapter per format** (`imazing_txt`, `whatsapp_txt`, `tabular` for CSV/XLSX), each
  yielding canonical `Message` records or `RecordIssue` findings. Format detection (`detect`)
  and checks shared by all formats (`pipeline`: control characters, duplicates, ordering,
  statistics) are separate modules.
- **Streaming everywhere**: files are read line by line or row by row; XLSX uses openpyxl's
  read-only mode. No intermediate CSV is created; the normalized CSV is an optional output.
- **No guessing**: column mappings come from presets whose columns all exist, mapping files or
  explicit `--map` flags; encodings other than UTF-8/UTF-16-with-BOM need `--encoding`;
  ambiguous day/month order needs `--date-order`; timestamps with offsets are rejected instead
  of converted.
- **Schema change**: `MediaKind` gains `contact` and `location`, which real exports contain
  (thousands of rows in the inspected CSV). The generator keeps using only file kinds, so M1
  output is unchanged.
- **Not imported**: reply quotes and reactions are removed from iMazing TXT text (the CSV keeps
  them in separate columns that are not imported either), counted in the report. Delivery
  status, forwarding flags and sender IDs are not imported.
- **Duplicates are kept by default** and reported; `--drop-duplicates` removes them. Exports
  with minute precision make legitimate repeats identical, so removal must be a choice.
- **All-or-nothing output**: the normalized CSV is written to a temporary file and moved into
  place only when every input file is accepted.
- **Formula protection** with a reversible apostrophe escape, undone only by the canonical
  mapping (see docs/input-formats.md).
- **Native WhatsApp TXT is experimental**: modeled on `export_formats`, tested with
  hand-written fixtures and generator output, not with real exports. A missing `--owner` is an
  error; an owner that never appears in a file is a warning, because chats where the owner
  never replied are legitimate.

## Consequences

- iMazing TXT and CSV of the same chats differ in conversation IDs (file name vs. chat title),
  sender names and media detail; the TXT cannot distinguish shared contacts and locations.
- Peak memory grows with the duplicate index (about 150 MB for a million messages).
- Questions from ADR 0002 about native Android exports (captions, narrow no-break space) remain
  open; the iMazing files do not answer them.
- iMazing TXT and CSV agree on about 95.2% of message texts and 98.7% of message types in a
  local check of 91 chats; the remaining differences are not fully characterized yet.

## Future improvements (out of scope for milestone 2)

- Optional preservation of reply quotes and reactions (for example as extra, non-canonical
  columns or a side table) instead of removing them.
- Characterize the remaining iMazing TXT/CSV differences.
- Validate the WhatsApp TXT adapter against real exports and lift its experimental status.
