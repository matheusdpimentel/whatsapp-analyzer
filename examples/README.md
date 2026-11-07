# Examples

Every file in this directory is **synthetic**: names, phone numbers (555-01XX), addresses
(example.com/example.org) and messages are invented. No real conversation was used.

| Directory | Content |
|-----------|---------|
| `demo-en/` | English conversations |
| `demo-pt-BR/` | Brazilian Portuguese conversations |

Each directory contains:

- `txt/<format>/chat_NNNN.txt`: one WhatsApp chat export per conversation, in the Android and
  iOS layouts for that language.
- `conversations.csv`: the same messages in the canonical column layout.
- `conversations_custom_columns.csv`: the same messages with different column names, a
  `from_me` flag and day-first timestamps, to try column mapping.
- `conversations.xlsx`: the canonical rows below a title row, in the `Messages` sheet.
- `manifest.json`: seed, generator version and the profile of each conversation.

The datasets are regenerated with:

```bash
uv run whatsapp-analyzer demo-data examples/demo-en --seed 7 --conversations 10 --locale en --overwrite
uv run whatsapp-analyzer demo-data examples/demo-pt-BR --seed 7 --conversations 10 --locale pt-BR --overwrite
```

`tests/test_examples.py` fails if the generator output changes without these files being
regenerated.
