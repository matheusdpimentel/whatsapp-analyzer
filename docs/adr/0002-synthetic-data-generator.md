# ADR 0002: Synthetic data generator

- Status: Accepted
- Date: 2026-11-07

## Context

The repository must not contain real conversations, yet the parser, metrics, demos, tests and
benchmarks all need realistic WhatsApp data, including awkward cases.

## Decision

- `whatsapp_analyzer.synthetic` generates conversations in a generic customer-support setting
  and ships with the package, exposed as `whatsapp-analyzer demo-data`.
- Output is deterministic. Each conversation has its own generator seeded with
  `"{seed}:{index}"`, so adding conversations never changes earlier ones, and timelines end at a
  fixed date instead of the current time. TXT, CSV and the manifest are byte-identical across
  runs and platforms; XLSX files only have identical cell contents, because the zip container
  stores write times.
- Conversations follow profiles (`engaged`, `responsive`, `one_sided`, `dormant`, `new_contact`,
  `burst`, `group`) that cover the engagement patterns the metrics must handle. The first
  conversations cycle through all profiles, so small datasets still cover every one.
- Two fixed conversations are always added: one with every message shape (all media kinds,
  captions, deleted and multiline messages, links, contact details, an auto-reply, short
  acknowledgements and same-minute messages), and one whose dates cannot reveal day/month order.
- Personal-looking values are reserved fictional ones: 555-01XX phone numbers and
  example.com/example.org addresses (RFC 2606). A test enforces this.
- WhatsApp export layouts are defined once in `whatsapp_analyzer.export_formats` and shared by
  the generator and the TXT parser. `expected_txt_records` describes what a parser can recover
  from each layout (Android drops seconds and media kinds), which gives the parser tests their
  expected values.
- `examples/` holds generated datasets, and `tests/test_examples.py` fails when the generator's
  output changes without them being regenerated.

## Consequences

- Not yet verified against real exports: whether Android exports made without media keep
  media captions (rendered here as a line after `<Media omitted>`), and which app versions put a
  narrow no-break space before AM/PM. Check both with a real export before the parser is released.
- Distinct sessions are at least 30 hours apart, longer than the default 24-hour session gap, and
  `burst` conversations use reply delays of a few minutes, so their shape matches their name.
- The four layouts model commonly observed exports, not an official specification. Real exports
  vary with app version and phone settings, so the parser must stay pattern-based, and new
  variants should be added to `export_formats.py` together with tests.
- The generator keeps a whole dataset in memory. That is fine for demos and tests; very large
  benchmark datasets may need a streaming writer later.
