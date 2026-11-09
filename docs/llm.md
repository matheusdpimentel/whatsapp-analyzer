# LLM core (offline)

Milestone 4 adds the provider-independent core for classifying conversations with a language
model. **Nothing in it contacts a network or a paid service.** The only provider is the
deterministic `FakeProvider`. Real OpenAI and Anthropic adapters arrive in milestone 6. The
runner, concurrency, retries and the persistence of calls arrive in milestone 5.

**Local mode sends nothing anywhere.** Importing, metrics, `llm prompts`, `llm preview` and
`llm preview --simulate` run entirely on this machine and work without internet access. No
content, metadata or telemetry is sent to any external API or service.

The semantic classification is independent from the deterministic engagement metrics
([metrics.md](metrics.md)). Metrics are not sent to the model, and the label never changes
or replaces a metric level.

## Three separate results

Every call produces three independent things.

| Axis | Values | Meaning |
|---|---|---|
| Execution status | `SUCCESS`, `INVALID_RESPONSE`, `REFUSED`, `CONTENT_FILTERED`, `INCOMPLETE_OUTPUT`, `FAILED` | What technically happened. `FAILED` carries an error kind. |
| Classification label | `HIGH`, `MEDIUM`, `LOW`, `REVIEW_REQUIRED` | Present only when the status is `SUCCESS`. |
| Security flags | `INJECTION_SUSPECTED`, `DELIMITER_IN_CONTENT` | Risk signals about the input. They never change the label. |

`REVIEW_REQUIRED` is a valid conclusion of the model: the conversation is ambiguous or too
short to decide. It never stands for a technical failure. An invalid or truncated answer, a
refusal and a provider error are statuses, never labels, so retries and monitoring can tell
them apart.

## Workflow

```bash
whatsapp-analyzer llm prompts                         # templates with versions and hashes
whatsapp-analyzer llm preview KEY -w ./analysis       # structure only: counts, estimates, hashes
whatsapp-analyzer llm preview KEY -w ./analysis --simulate        # + offline fake provider run
whatsapp-analyzer llm preview KEY -w ./analysis --show-content    # asks for confirmation
whatsapp-analyzer llm preview KEY -w ./analysis --out ./analysis/preview.md --confirm-content
```

`KEY` is the conversation key as imported (for TXT files, the file name without its extension).
Use `--source-format` when the same key exists in several formats.

### Preview safety

- By default the preview shows **no content**: message counts, omissions and truncations,
  masking counts by type, estimated tokens, the cost upper bound, security flags and hashes.
- `--show-content` prints the masked prompt and `--out FILE` writes it. Both require explicit
  confirmation: typing `yes` in an interactive terminal, or `--confirm-content` when there is no
  terminal (scripts, agents, `--json`). `--confirm-content` confirms content output only. It
  does not disable the other checks.
- `--out` writes with mode 0600 on macOS and Linux. On Windows, permissions follow the
  directory, so keep the file in a private folder. The file is created inside the directory
  that was checked: on macOS and Linux that directory is opened once without following
  symbolic links, its device and inode must match the ones recorded at check time, and the
  file is created relative to it. If the directory was moved or replaced after the check,
  nothing is written (`OUTPUT_LOCATION_CHANGED`). If it is moved after that last check, the
  file still lands in the directory that was checked, never in the new one. Whether this
  protected write is available is detected by trying the exact operations once (open, create,
  replace and unlink relative to a directory) in a private temporary directory, because
  `os.supports_dir_fd` under-reports them on macOS. Windows has no equivalent API, so there the
  identity is checked again just before writing by path. A swap between that check and the
  write remains possible there; keep `--out` files in a directory only you can change. It never replaces an existing file without `--overwrite`. It
  refuses symbolic links, and it refuses locations that Git could commit. Git itself decides:
  inside a work tree, the target must be reported as ignored by `git check-ignore`, which applies
  negations (`!file`), nested and global ignore files and `.git/info/exclude`, and never treats
  a tracked file as ignored. Inside a workspace (whose `.gitignore` is `*`) this normally
  holds. If `git` is not installed, any `.git` above the target makes it unsafe.
  `--allow-git-worktree` lifts only this check. The location is checked again right before writing, after the
  confirmation.
- Content is never logged. The original, unmasked text is never shown.

## Context preparation

Settings live in `analyzer.toml`:

```toml
[llm]
provider = "fake"                   # the only provider in milestone 4
model = "fake-classifier"
prompt = "engagement-classification"   # id, id@version or a .md file

[llm.context]
max_messages = 200
max_context_tokens = 6000           # estimated tokens of the conversation block
max_message_chars = 1000
include_groups = false
```

Rules:

1. The context uses the current version of the conversation, in the canonical order
   (timestamp, source position). System messages and `[metrics] ignored_texts` are left out.
   Group chats are excluded unless `include_groups = true`. Group detection is a heuristic
   (two or more named inbound senders, see [metrics.md](metrics.md)), so a group in which only
   one member wrote to the owner is treated as a one-to-one chat.
2. Every eligible message gets an id (`m1`, `m2`, and so on) in that order. Ids do not change
   with the selection, so evidence ids are stable.
3. Senders appear only as `OWNER` or `CONTACT`, never by name. Media appears as
   `[media: audio]` (plus the caption) and deleted messages as `[deleted message]`.
4. Text is normalized to NFC, and every kind of line break (`\r`, `\v`, `\f`, `\x85`,
   `\u2028`, and so on) becomes `\n`. Then one canonical form is computed, and every match and
   replacement uses it: reserved private-use characters and invisible format characters
   (zero-width spaces and joiners, word joiner, byte order mark, soft hyphen, bidirectional
   controls) are removed first, and only then is the text normalized to NFC. Invisible
   characters therefore cannot split a number or a name, and no later normalization can shift
   positions. As a side effect, emoji sequences joined by zero-width joiners appear as separate
   emoji. Identifiers are masked on this raw text, so names and numbers are matched as
   written. Placeholders are first inserted as private-use sentinel
   characters (removed from the input beforehand, so content cannot forge one). Then `&`, `<`,
   `>` and `[` are escaped (`&amp;`, `&lt;`, `&gt;`, `&#91;`), and only then do the sentinels
   become `[KIND_n]`. Escaping therefore never splits a placeholder, and a masked name never
   breaks an entity. Content can never close the `<conversation>` block or imitate a marker
   or placeholder. Continuation lines are indented, so content can never start a line that
   looks like a new message.
5. A message longer than `max_message_chars` (measured after escaping and masking) is cut, with
   `[truncated: message had N chars]`, where N is the original length. Placeholders and entities
   are never split. Raw text beyond 4 × `max_message_chars` (at most 16,000 characters) is dropped before masking, so
   preparation time stays bounded for very long messages.
6. The most recent messages that fit `max_messages` and `max_context_tokens` are kept, and
   `[N earlier messages omitted]` replaces the rest. The marker counts toward the budget, so the
   block never exceeds it. If even the latest message and that notice do not fit, preparation
   fails with `CONTEXT_BUDGET_EXCEEDED`. If not even the latest message fits, the
   preparation fails with `CONTEXT_BUDGET_EXCEEDED`; nothing is cut silently.
7. `context_hash` covers the prepared text, the context settings and the masking version.

### Masking is not anonymization

The masker replaces, with placeholders numbered per distinct value within a conversation:

- e-mail addresses;
- URLs, in full and including the domain: with a scheme or `www.`, any host name followed by
  a path or query, and bare lowercase host names that contain a common top-level domain label
  ("Oi.Me liga" is left alone). A host name is always masked whole, never in part
  (`loja.example.com.evil` becomes one placeholder). URLs are recognized before e-mail
  addresses, and a URL that contains an address in its userinfo, path or query becomes a
  single placeholder, so nothing after the address stays visible;
- CPF/CNPJ-shaped numbers;
- phone numbers (8 to 15 digits in any script, excluding date-like values), with any spacing
  (also no-break and narrow spaces), dots, parentheses or dashes (also non-breaking, en and
  full-width ones), even when letters touch them ("tel48999991111"). Longer runs, such as two
  numbers in a row or a card number, are masked whole as `[NUMBER_n]`, so no digit stays
  visible;
- other runs of 6 or more digits;
- @mentions;
- known names, in full and by their parts, as `[CONTACT_NAME]` and `[OWNER_NAME]`. They are
  matched without regard to case or accents ("Jose" matches "José"). Contact names are the
  senders of every inbound message of the conversation, even excluded ones, plus the
  conversation key (often the chat title or file name), with known export prefixes such as
  "WhatsApp Chat with" removed. Particles and words common in chat titles ("com", "with",
  "chat", "grupo", and so on) are never masked on their own. Owner names are the outbound senders.
  A contact who never wrote and whose name is not in the key is not known.

This is **best-effort pseudonymization of common patterns, not guaranteed anonymization**.
Masking is an additional safeguard, not a privacy guarantee. It handles the separators and
invisible characters listed above, but it does not defeat every form of Unicode obfuscation
(for example digits spelled out as words, or look-alike characters from other scripts). Names
of other people, addresses, unusual identifiers, and the conversation's own context remain.

Known limitations, accepted for milestone 4:

- **Known names in some Unicode representations** can stay partly or fully visible. Examples
  are names written with look-alike characters, unusual combining sequences, or a spelling that
  differs from the sender name the export recorded.
- **Normalization changes the text slightly.** NFC composes accents. Removing invisible
  characters splits emoji sequences joined by zero-width joiners (a family emoji appears as
  separate emoji), and can change how some scripts that use joiners are displayed (for example
  Indic and Arabic text). The meaning of ordinary messages is kept. The masked text still leaves the
machine when it is sent to a provider in later milestones. The mapping from placeholders back
to values exists only in memory and is never sent, printed or stored.

### Prompt injection

Messages are untrusted data. The defenses work together:

- The system instructions and the conversation are kept apart. The system section declares the
  conversation to be data, never instructions.
- Delimiters are escaped (rule 4 above), and fake message lines cannot be injected.
- The output schema is closed. Evidence must reference ids that were actually sent; `HIGH` and
  `MEDIUM` must cite at least one of the contact's messages; and the response may not quote
  text. These rules **limit** what a successful injection could produce. They do not detect
  one: a well-formed answer steered by injected text still validates.
- Heuristics look for typical patterns ("ignore previous instructions", role prefixes such as
  `system:`, block tags, ready-made JSON answers) in English and Portuguese. A match only raises
  `INJECTION_SUSPECTED` with the ids of the shown messages involved. It is a **risk signal, not
  proof**. People legitimately discuss instructions, prompts and code, so a flag never changes
  the label or blocks a call. It is reported for review, apart from the classification.

## Prompt templates

Templates are Markdown files with a TOML front matter:

```markdown
+++
id = "engagement-classification"
version = "1.0.0"
description = "..."
output_schema = "engagement-output/v1"
variables = ["conversation", "message_count", "omitted_count", "truncated_count"]
max_output_tokens = 300
+++

## system
...

## user
...{{conversation}}...
```

- **Sections**: only `## system` and `## user`. There is no logic and there are no
  conditionals.
- **Variables**: placeholders are `{{name}}`. The declared variables must equal the
  placeholders used, and rendering needs exactly those values. Values are inserted in one pass,
  so a message containing `{{...}}` is never expanded again.
- **Hashes**: `template_hash` is the SHA-256 of the normalized file (LF, no BOM).
  `prompt_hash` covers the rendered text, the template identity, the model and the output
  limit. `request_id` hashes the prompt hash, context hash, model and output schema.
- **Built-in templates** are listed in `src/whatsapp_analyzer/llm/prompts/manifest.toml` with
  their hashes, and a test checks them. A released template is never edited in place: a change
  means a new version file and a new manifest entry.
- **Custom templates**: pass `--prompt FILE.md` (relative to the current directory), set
  `[llm] prompt = "file.md"` (relative to the workspace, wherever the command runs), or put
  templates in `<workspace>/prompts/` and refer to them by id. An invalid file there is
  skipped, never blocking the valid ones; `llm prompts` lists it with its error, and asking for
  a template that only an invalid file could provide reports those files. Duplicate
  `id@version` pairs of the requested template are rejected.

## Output schema (`engagement-output/v1`)

```json
{"level": "HIGH", "reasons": ["ASKS_QUESTIONS"], "evidence": ["m12"]}
```

- `level`: `HIGH`, `MEDIUM`, `LOW` or `REVIEW_REQUIRED`.
- `reasons`: one to five distinct codes from a closed list of observable behaviors only:
  `ASKS_QUESTIONS`, `CONFIRMS_OR_AGREES`, `REQUESTS_FOLLOW_UP`, `SHORT_ACKNOWLEDGEMENTS_ONLY`,
  `NO_REPLY`, `DECLINES`, `ASKS_TO_STOP_CONTACT`, `AUTOMATED_REPLY`, `OFF_TOPIC`,
  `INSUFFICIENT_CONTEXT`.
- `evidence`: up to five distinct ids of messages that were in the context. `HIGH` and
  `MEDIUM` must include at least one id of a contact message (`EVIDENCE_REQUIRED` otherwise).

There are no free-text fields and no self-reported confidence. Each request receives its own
copy of the schema (`output_schema()`), so no caller can change it for later requests. Validation is strict: one JSON
object (optionally in a single fenced block) with no duplicate keys, no `NaN`, no extra or
missing fields and at most 4000 characters. Failures have stable codes: `NOT_JSON`,
`DUPLICATE_KEY`, `NOT_AN_OBJECT`, `MISSING_FIELD`, `UNKNOWN_FIELD`, `INVALID_VALUE`,
`UNKNOWN_EVIDENCE`, `EVIDENCE_REQUIRED` and `TOO_LONG`. Pathological input, such as deeply
nested arrays, is reported as `NOT_JSON` and never crashes the caller.

## Tokens and costs

### Usage buckets

| Field | Meaning |
|---|---|
| `input_uncached` | input billed at the normal rate |
| `input_cache_read` | input read from a prompt cache |
| `input_cache_write` | input written to a prompt cache |
| `output_total` | every billed output token, **reasoning included** |
| `output_reasoning` | the part of `output_total` spent on reasoning; informational |
| `estimated` | `true` for local estimates, `false` for provider-reported counts |

The buckets are disjoint, so input total is the sum of the three input buckets. Reasoning
tokens are never added to `output_total` again. `null` means "not reported" and is never
treated as zero. Real adapters (milestone 6) translate each provider's usage into these buckets,
and each translation will be tested.

### Estimates

Local token counts are **estimates**: about one token per 3 characters plus a fixed overhead per
message, which is deliberately conservative. They drive the context budget and the cost upper
bound in the preview. They are always labeled as estimates and are never presented as exact
counts.

### Prices

Prices are per million tokens, written as decimal strings and computed with `Decimal`. Each
entry records `effective_from` and `source`:

```toml
schema = 1
[[prices]]
provider = "acme"
model = "acme-small"
currency = "USD"
unit = "per_million_tokens"
input = "1.00"
cache_read = "0.10"     # optional
cache_write = "1.25"    # optional
output = "4.00"
effective_from = 2026-06-01
source = "https://example.com/pricing (retrieved 2026-06-01)"
```

The built-in table contains only the zero-priced fake model. Milestone 6 adds real prices with
their source and retrieval date. `<workspace>/pricing.toml` can add or override entries at any
time; it wins over a built-in entry with the same effective date. The preview uses the entry in
effect today; entries dated in the future are ignored until their date. Dates must be plain
TOML dates, not date-times.

A cost is never guessed. If a bucket has tokens but no price, or a usage count is unknown, the
total is `null` and `missing` lists what is lacking. Tiered context pricing, batch discounts and
cache durations with different prices are not modeled yet; models that need them have an
unknown cost until milestone 6.

## Provider contract

```python
class Provider(Protocol):
    @property
    def name(self) -> str: ...
    async def complete(self, request: LLMRequest) -> LLMResult: ...  # raises ProviderError
```

- The interface is asynchronous; see ADR 0005 for the comparison with threads.
- `classify()` wraps a call with a timeout and maps every outcome onto the three axes above.
  Cancellation propagates to the caller.
- The contract is independent of the CLI, the database and the future runner.

### Error kinds

| Kind | Retryable | Account error (exit 5 in later milestones) |
|---|---|---|
| `AUTHENTICATION`, `PERMISSION_DENIED`, `INSUFFICIENT_CREDITS`, `MODEL_NOT_FOUND` | no | yes |
| `RATE_LIMITED`, `OVERLOADED`, `SERVER_ERROR`, `TIMEOUT`, `NETWORK`, `MALFORMED_PROVIDER_RESPONSE` | yes | no |
| `CONTEXT_TOO_LONG`, `INVALID_REQUEST` | no | no |
| `ADAPTER_ERROR` (an unexpected exception in an adapter: a bug) | no | no |

`ProviderError.safe_message` is redacted. Values of the credential variables and key-shaped
strings (`sk-...`, `Bearer ...`, `api_key=...`) are replaced, and request content and headers
are never included. Adapters must never suppress `asyncio.CancelledError`. Real adapters
(milestone 6) will hold credentials in `SecretValue`, whose `repr` is `SecretValue('***')`.
The fake provider's usage is a local estimate and is labeled `estimated: true`.

## Telemetry

`CallTelemetry` records:

- identifiers: the request id and the provider request id;
- the provider and model (the configured one and the one the provider reported);
- the prompt id, version and hash, and the context hash;
- the status, label, finish reason, error kind, HTTP status, retryability and validation error
  code;
- the security flags, the usage buckets, the cost estimate and the latency.

It never contains prompts, message text, raw responses or credentials. Milestone 4 keeps it in
memory; milestone 5 persists it.

## Requirement for milestone 6: explicit authorization before sending

Before any real provider sends conversation content outside this machine, milestone 6 must
require an explicit authorization from the user:

- It names the provider and model and states that masked conversation content will be sent.
- It is given per workspace and provider, and recorded with a hash of what was authorized, so a
  change of provider, model or prompt requires authorizing again.
- It works in non-interactive runs (scripts, agents, `--json`) through an explicit,
  provider-specific option or setting. It is never implied by a generic "yes" flag.
- It only authorizes sending. It never disables the other controls: masking, preview
  protections, budgets, rate limits, Git checks and redaction stay in force.
- Without it, no content leaves the machine and the command fails with a clear error.

## Not in milestone 4

Real providers and paid calls (milestone 6); runner, scheduling, queues, concurrency limits,
retries and persistence of calls (milestone 5); second-model review and consolidation
(milestone 7); calibration and human feedback (milestone 8); optionally sending deterministic
metrics to the model, which can be added later as an explicit, hashed template variable.
