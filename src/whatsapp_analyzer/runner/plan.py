"""Run identity, frozen scope and job planning.

- `run_key` hashes everything that shapes the requests: provider, model, template (id,
  version, hash), context settings, masking version, ignored texts, output schema and
  generation parameters. The `request_id` of each job (from `llm.call.build_request`) covers
  the rendered prompt, the context hash, the model, the output schema, `max_output_tokens`,
  `temperature` and provider-specific parameters (options, structured output mode).
- A job whose estimated prompt cannot fit the model's known context window (with a safety
  margin, because local estimates differ from each provider's tokenizer) is skipped with
  `MODEL_CONTEXT_EXCEEDED` instead of being sent. Unknown limits are not checked. When a
  price entry has a tier ceiling (`max_input_tokens`) but no long-context rates, a job whose
  prompt may exceed it is skipped with `PRICE_TIER_EXCEEDED`, so it is never costed at a
  too-low rate; with long rates, the tier is chosen per request (`llm.pricing`).
- The scope is frozen when a run is planned: one job per selected conversation version, by
  immutable version id. Later imports create new versions and never change an existing run.
- Running again with the same key and the same scope resumes the latest such run (or reports
  it up to date). `--force` plans a new run instead.
"""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from whatsapp_analyzer import __version__
from whatsapp_analyzer.config import ContextSettings, LimitSettings
from whatsapp_analyzer.hashing import canonical_json, config_hash, sha256_hex
from whatsapp_analyzer.llm.call import DEFAULT_TIMEOUT_SECONDS, Generation, build_request
from whatsapp_analyzer.llm.catalog import ModelInfo
from whatsapp_analyzer.llm.context import ContextError, PreparedContext, build_context
from whatsapp_analyzer.llm.masking import MASKING_VERSION, name_from_key
from whatsapp_analyzer.llm.output import OUTPUT_SCHEMA_ID
from whatsapp_analyzer.llm.pricing import LONG_RATE_FIELDS, Price
from whatsapp_analyzer.llm.prompts import PromptTemplate
from whatsapp_analyzer.llm.tokens import estimate_prompt_tokens
from whatsapp_analyzer.llm.types import LLMRequest
from whatsapp_analyzer.metrics.raw import StoredMessage
from whatsapp_analyzer.metrics.runner import messages_of
from whatsapp_analyzer.runner.model import JobStatus, RunStatus
from whatsapp_analyzer.store.db import transaction, utc_now

RUNNER_VERSION = 1
PLAN_BATCH = 500
CONTEXT_SAFETY_MARGIN = 1.25  # estimated prompt tokens are multiplied by this before checking


@dataclass(frozen=True)
class RunConfig:
    provider: str
    model: str
    template: PromptTemplate
    context: ContextSettings
    ignored_texts: tuple[str, ...]
    limits: LimitSettings
    price: Price | None
    generation_settings: Generation = Generation()  # noqa: RUF009  (frozen, immutable)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    model_info: ModelInfo | None = None  # known limits of the model, for the context check

    @property
    def max_output_tokens(self) -> int:
        return self.generation_settings.max_output_tokens or self.template.max_output_tokens

    def context_config_hash(self) -> str:
        return config_hash(
            "llm-context",
            MASKING_VERSION,
            {"settings": asdict(self.context), "ignored_texts": sorted(set(self.ignored_texts))},
        )

    def generation(self) -> dict[str, object]:
        values: dict[str, object] = {
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.generation_settings.temperature,
            "output_schema": OUTPUT_SCHEMA_ID,
        }
        if self.generation_settings.params:
            values["provider_params"] = dict(self.generation_settings.params)
        return values

    def run_key(self) -> str:
        return config_hash(
            "llm-run",
            RUNNER_VERSION,
            {
                "provider": self.provider,
                "model": self.model,
                "template": [self.template.id, self.template.version, self.template.template_hash],
                "context": self.context_config_hash(),
                "generation": self.generation(),
            },
        )


@dataclass(frozen=True)
class Selected:
    conversation_id: int
    version_id: int
    conversation_key: str
    is_group: bool


@dataclass(frozen=True)
class PreparedJob:
    context: PreparedContext
    request: LLMRequest


class RunConfigChangedError(Exception):
    pass


class NothingSelectedError(Exception):
    pass


def select_versions(connection: sqlite3.Connection, keys: Sequence[str] | None) -> list[Selected]:
    rows = connection.execute(
        "SELECT c.id, v.id, c.conversation_key, v.is_group FROM conversations c "
        "JOIN conversation_versions v ON v.id = c.current_version_id ORDER BY c.id"
    ).fetchall()
    wanted = set(keys) if keys else None
    return [
        Selected(row[0], row[1], row[2], bool(row[3]))
        for row in rows
        if wanted is None or row[2] in wanted
    ]


def scope_fingerprint(selection: Sequence[Selected]) -> str:
    return sha256_hex(canonical_json(sorted([s.conversation_id, s.version_id] for s in selection)))


def prepare(config: RunConfig, selected: Selected, messages: list[StoredMessage]) -> PreparedJob:
    """Context and request for one job. Deterministic: the same version and configuration
    always give the same context hash and request id."""
    context = build_context(
        messages,
        config.context,
        is_group=selected.is_group,
        ignored_texts=config.ignored_texts,
        contact_names=[name_from_key(selected.conversation_key)],
    )
    _, request = build_request(
        config.template,
        context,
        config.model,
        timeout_seconds=config.timeout_seconds,
        generation=config.generation_settings,
    )
    if config.model_info is not None and not fits_model(request, config.model_info):
        raise ContextError("MODEL_CONTEXT_EXCEEDED")
    price = config.price
    if (
        price is not None
        and price.max_input_tokens is not None
        and not price.has_long_rates
        and _padded_input(request) > price.max_input_tokens
    ):
        # A higher tier would apply and the entry cannot price it: skip, never under-cost.
        raise ContextError("PRICE_TIER_EXCEEDED")
    return PreparedJob(context, request)


def _padded_input(request: LLMRequest) -> int:
    estimated = estimate_prompt_tokens(request.system, [m.content for m in request.messages])
    return int(estimated * CONTEXT_SAFETY_MARGIN)


def fits_model(request: LLMRequest, info: ModelInfo) -> bool:
    """False when the request certainly or probably exceeds a known limit of the model."""
    padded = _padded_input(request)
    if info.max_input_tokens is not None and padded > info.max_input_tokens:
        return False
    return info.context_window is None or padded + request.max_output_tokens <= info.context_window


def iter_prepared(
    connection: sqlite3.Connection, config: RunConfig, selection: Sequence[Selected]
) -> Iterator[tuple[Selected, PreparedJob | ContextError]]:
    for start in range(0, len(selection), PLAN_BATCH):
        batch = selection[start : start + PLAN_BATCH]
        loaded = dict(messages_of(connection, [s.version_id for s in batch]))
        for selected in batch:
            try:
                yield selected, prepare(config, selected, list(loaded.get(selected.version_id, [])))
            except ContextError as exc:
                yield selected, exc


def find_run(
    connection: sqlite3.Connection, config: RunConfig, fingerprint: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM llm_runs WHERE run_key = ? AND scope_fingerprint = ? "
        "ORDER BY id DESC LIMIT 1",
        (config.run_key(), fingerprint),
    ).fetchone()
    return row


def load_run(connection: sqlite3.Connection, run_id: int, config: RunConfig) -> sqlite3.Row:
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM llm_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise LookupError(run_id)
    if row["run_key"] != config.run_key():
        raise RunConfigChangedError(run_id)
    return row


def create_run(
    connection: sqlite3.Connection, config: RunConfig, selection: Sequence[Selected]
) -> int:
    """Plan a run in one transaction: the run row and one job per selected version."""
    if not selection:
        raise NothingSelectedError
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO llm_runs (status, run_key, scope_fingerprint, provider, model, "
            "prompt_id, prompt_version, template_hash, context_config_hash, generation_json, "
            "limits_json, price_json, jobs_total, tool_version, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                RunStatus.RUNNING.value,
                config.run_key(),
                scope_fingerprint(selection),
                config.provider,
                config.model,
                config.template.id,
                config.template.version,
                config.template.template_hash,
                config.context_config_hash(),
                canonical_json(config.generation()),
                limits_json(config.limits),
                price_json(config.price),
                len(selection),
                __version__,
                utc_now(),
            ),
        )
        assert cursor.lastrowid is not None
        run_id = cursor.lastrowid
        rows: list[tuple[object, ...]] = []
        for selected, prepared in iter_prepared(connection, config, selection):
            if isinstance(prepared, ContextError):
                rows.append(
                    (
                        run_id,
                        selected.version_id,
                        JobStatus.SKIPPED.value,
                        prepared.kind,
                        None,
                        None,
                        None,
                    )
                )
            else:
                rows.append(
                    (
                        run_id,
                        selected.version_id,
                        JobStatus.PENDING.value,
                        None,
                        prepared.request.request_id,
                        prepared.context.context_hash,
                        prepared.request.prompt_hash,
                    )
                )
        connection.executemany(
            "INSERT INTO llm_jobs (run_id, version_id, status, skip_reason, request_id, "
            "context_hash, prompt_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        mark_used(connection, run_id)
    return run_id


def mark_used(connection: sqlite3.Connection, run_id: int) -> None:
    connection.execute(
        "UPDATE llm_runs SET last_used_seq = "
        "(SELECT coalesce(max(last_used_seq), 0) + 1 FROM llm_runs) WHERE id = ?",
        (run_id,),
    )


def limits_json(limits: LimitSettings) -> str:
    values = asdict(limits)
    values["max_cost"] = None if limits.max_cost is None else str(limits.max_cost)
    return canonical_json(values)


def price_json(price: Price | None) -> str | None:
    if price is None:
        return None
    return canonical_json(
        {
            "provider": price.provider,
            "model": price.model,
            "currency": price.currency,
            "input": str(price.input),
            "output": str(price.output),
            "cache_read": None if price.cache_read is None else str(price.cache_read),
            "cache_write": None if price.cache_write is None else str(price.cache_write),
            "effective_from": price.effective_from.isoformat(),
            "source": price.source,
            "origin": price.origin,
            "retrieved_at": price.retrieved_at.isoformat() if price.retrieved_at else None,
            "max_input_tokens": price.max_input_tokens,
            **{
                name: None if getattr(price, name) is None else str(getattr(price, name))
                for name in LONG_RATE_FIELDS
            },
        }
    )


def price_from_json(text: str | None) -> Price | None:
    """The price snapshot of a run: costs of a resumed run use the price it started with."""
    if text is None:
        return None
    data = json.loads(text)

    def rate(name: str) -> Decimal | None:
        value = data[name]
        return None if value is None else Decimal(value)

    input_rate, output_rate = rate("input"), rate("output")
    assert input_rate is not None and output_rate is not None
    return Price(
        provider=data["provider"],
        model=data["model"],
        currency=data["currency"],
        input=input_rate,
        output=output_rate,
        cache_read=rate("cache_read"),
        cache_write=rate("cache_write"),
        effective_from=date.fromisoformat(data["effective_from"]),
        source=data["source"],
        origin=data["origin"],
        retrieved_at=date.fromisoformat(data["retrieved_at"]) if data.get("retrieved_at") else None,
        max_input_tokens=data.get("max_input_tokens"),
        **{
            name: None if data.get(name) is None else Decimal(data[name])
            for name in LONG_RATE_FIELDS
        },
    )
