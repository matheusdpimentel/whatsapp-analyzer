"""Workspace configuration (`analyzer.toml`).

The file has independent sections:

- `[metrics]` controls how RAW metrics are computed from messages. Changing it (or the resolved
  reference date) produces a new metrics hash, and raw metrics are computed again.
- `[scoring]` controls how raw metrics become normalized values, a score and a level. Changing
  it only re-scores the stored raw metrics.
- `[llm]` selects the provider, model and prompt template, and `[llm.context]` limits what is
  prepared for a model. It never affects deterministic metrics.

Validation is strict: unknown keys, wrong types and out-of-range values are errors, never
silently corrected. Missing keys take the documented defaults.
"""

import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

DATASET_MAX = "dataset_max"
MIN_REFERENCE_YEAR = 1900
MAX_REFERENCE_YEAR = 9000
METRICS_VERSION = 1  # bump when raw metric definitions change
SCORING_VERSION = 1  # bump when normalization or classification rules change


class DuplicatePolicy(StrEnum):
    KEEP = "keep"  # every stored record counts
    COLLAPSE = "collapse"  # exact repeats count once (first in canonical order)


class ConfigError(Exception):
    """`kind` selects the message; `details` fill it in (key paths and limits only)."""

    def __init__(self, kind: str, **details: object) -> None:
        super().__init__(kind)
        self.kind = kind
        self.details = details


@dataclass(frozen=True)
class MetricsSettings:
    reference_date: date | None = None  # None: the latest message date in the workspace
    lookback_weeks: int = 26
    reply_window_hours: float = 168.0
    session_gap_hours: float = 24.0
    ignored_texts: tuple[str, ...] = ("(Waiting for this message)",)
    duplicates: DuplicatePolicy = DuplicatePolicy.KEEP
    count_deleted_as_activity: bool = True

    def resolved(self, reference_date: date) -> dict[str, object]:
        """Everything raw metrics depend on, with the reference date resolved. Hashed."""
        return {
            "reference_date": reference_date.isoformat(),
            "lookback_weeks": self.lookback_weeks,
            "reply_window_hours": self.reply_window_hours,
            "session_gap_hours": self.session_gap_hours,
            "ignored_texts": sorted({text.strip() for text in self.ignored_texts}),
            "duplicates": self.duplicates.value,
            "count_deleted_as_activity": self.count_deleted_as_activity,
        }


@dataclass(frozen=True)
class Weights:
    frequency: float = 0.25
    reciprocity: float = 0.35
    initiative: float = 0.15
    recency: float = 0.25


@dataclass(frozen=True)
class ScoringSettings:
    weights: Weights = field(default_factory=Weights)
    target_active_weeks: int = 8
    recency_half_life_days: float = 90.0
    min_sessions_for_initiative: int = 2
    min_coverage: float = 0.5
    high_threshold: float = 0.66
    medium_threshold: float = 0.33
    high_min_inbound_content_messages: int = 3

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


SUPPORTED_PROVIDERS = ("fake",)  # real providers arrive in milestone 6


@dataclass(frozen=True)
class ContextSettings:
    max_messages: int = 200
    max_context_tokens: int = 6000  # estimated tokens for the conversation block
    max_message_chars: int = 1000
    include_groups: bool = False


@dataclass(frozen=True)
class LimitSettings:
    """Runner limits. `None` means "no limit". Token limits use local estimates for
    reservations, so they are conservative guides, not guarantees, for real providers."""

    concurrency: int = 4
    requests_per_minute: int | None = 60
    tokens_per_minute: int | None = None
    max_attempts: int = 3
    max_tokens: int | None = None
    max_cost: Decimal | None = None


@dataclass(frozen=True)
class LlmSettings:
    provider: str = "fake"
    model: str = "fake-classifier"
    prompt: str = "engagement-classification"  # template id, id@version or a .md path
    context: ContextSettings = field(default_factory=ContextSettings)
    limits: LimitSettings = field(default_factory=LimitSettings)


@dataclass(frozen=True)
class Settings:
    metrics: MetricsSettings = field(default_factory=MetricsSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)


DEFAULT_CONFIG_TEXT = """\
# whatsapp-conversation-analyzer workspace configuration.
# See docs/metrics.md for the definition of every value.

[metrics]
# Changing anything in this section recomputes raw metrics.
# "dataset_max" uses the date of the latest message in the workspace; a fixed date
# ("2026-01-31") keeps results stable when newer exports are imported.
reference_date = "dataset_max"
lookback_weeks = 26
reply_window_hours = 168
session_gap_hours = 24
# Exact texts that exporters write instead of a real message; they are not counted.
ignored_texts = ["(Waiting for this message)"]
# "keep" counts every stored record; "collapse" counts exact repeats once.
duplicates = "keep"
# Deleted messages count as activity (a message was sent), never as message content.
count_deleted_as_activity = true

[scoring]
# Changing anything in this section only re-scores the stored raw metrics.
target_active_weeks = 8
recency_half_life_days = 90
min_sessions_for_initiative = 2
min_coverage = 0.5
high_min_inbound_content_messages = 3

[scoring.weights]
frequency = 0.25
reciprocity = 0.35
initiative = 0.15
recency = 0.25

[scoring.thresholds]
high = 0.66
medium = 0.33

[llm]
# Milestone 4 is offline: only the "fake" provider exists. See docs/llm.md.
provider = "fake"
model = "fake-classifier"
prompt = "engagement-classification"

[llm.context]
max_messages = 200
max_context_tokens = 6000   # estimated, not exact
max_message_chars = 1000
include_groups = false

[llm.limits]
# Runner limits (see docs/runner.md). tokens_per_minute, max_tokens and max_cost have no
# limit when omitted; concurrency, requests_per_minute and max_attempts use these defaults.
concurrency = 4
requests_per_minute = 60
# tokens_per_minute = 100000
max_attempts = 3            # per job, only for retries that are safe
# max_tokens = 200000       # whole run, conservative local estimates
# max_cost = "1.00"         # whole run; needs a complete price entry
"""


def load_settings(path: Path) -> Settings:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError("missing_file", path=str(path)) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("unreadable", path=str(path)) from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("syntax", path=str(path), reason=str(exc)) from exc
    return parse_settings(data)


def parse_settings(data: dict[str, Any]) -> Settings:
    _reject_unknown(data, {"metrics", "scoring", "llm"}, "")
    metrics = _parse_metrics(_table(data, "metrics", ""))
    scoring = _parse_scoring(_table(data, "scoring", ""))
    llm = _parse_llm(_table(data, "llm", ""))
    if scoring.target_active_weeks > metrics.lookback_weeks:
        raise ConfigError(
            "target_exceeds_lookback",
            key="scoring.target_active_weeks",
            limit_key="metrics.lookback_weeks",
        )
    return Settings(metrics, scoring, llm)


def _parse_llm(table: dict[str, Any]) -> LlmSettings:
    path = "llm."
    defaults = LlmSettings()
    _reject_unknown(table, {"provider", "model", "prompt", "context", "limits"}, path)
    provider = table.get("provider", defaults.provider)
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            "invalid_choice", key=f"{path}provider", choices=", ".join(SUPPORTED_PROVIDERS)
        )
    for name in ("model", "prompt"):
        value = table.get(name, getattr(defaults, name))
        if not isinstance(value, str) or not value.strip():
            raise ConfigError("wrong_type", key=f"{path}{name}", expected="string")
    context = _table(table, "context", path)
    context_path = f"{path}context."
    base = ContextSettings()
    _reject_unknown(context, set(asdict(base)), context_path)
    return LlmSettings(
        provider=provider,
        model=table.get("model", defaults.model),
        prompt=table.get("prompt", defaults.prompt),
        context=ContextSettings(
            max_messages=_integer(
                context, "max_messages", context_path, base.max_messages, 1, 5000
            ),
            max_context_tokens=_integer(
                context, "max_context_tokens", context_path, base.max_context_tokens, 100, 1_000_000
            ),
            max_message_chars=_integer(
                context, "max_message_chars", context_path, base.max_message_chars, 20, 100_000
            ),
            include_groups=_boolean(context, "include_groups", context_path, base.include_groups),
        ),
        limits=_parse_limits(_table(table, "limits", path), f"{path}limits."),
    )


def _parse_limits(table: dict[str, Any], path: str) -> LimitSettings:
    base = LimitSettings()
    _reject_unknown(table, set(asdict(base)), path)

    def optional_integer(name: str, default: int | None, maximum: int) -> int | None:
        if name not in table:
            return default
        return _integer(table, name, path, 0, 1, maximum)

    max_cost = None
    if "max_cost" in table:
        value = table["max_cost"]
        if not isinstance(value, str):
            raise ConfigError(
                "wrong_type", key=f"{path}max_cost", expected='decimal string, e.g. "1.50"'
            )
        try:
            max_cost = Decimal(value)
        except InvalidOperation as exc:
            raise ConfigError(
                "wrong_type", key=f"{path}max_cost", expected="decimal string"
            ) from exc
        if not max_cost.is_finite() or max_cost < 0:
            raise ConfigError("out_of_range", key=f"{path}max_cost", minimum=0, maximum="any")
    return LimitSettings(
        concurrency=_integer(table, "concurrency", path, base.concurrency, 1, 64),
        requests_per_minute=optional_integer(
            "requests_per_minute", base.requests_per_minute, 100_000
        ),
        tokens_per_minute=optional_integer(
            "tokens_per_minute", base.tokens_per_minute, 100_000_000
        ),
        max_attempts=_integer(table, "max_attempts", path, base.max_attempts, 1, 10),
        max_tokens=optional_integer("max_tokens", base.max_tokens, 10_000_000_000),
        max_cost=max_cost,
    )


def _parse_metrics(table: dict[str, Any]) -> MetricsSettings:
    path = "metrics."
    defaults = MetricsSettings()
    _reject_unknown(table, set(asdict(defaults)), path)
    reference = table.get("reference_date", DATASET_MAX)
    return MetricsSettings(
        reference_date=_reference_date(reference, f"{path}reference_date"),
        lookback_weeks=_integer(table, "lookback_weeks", path, defaults.lookback_weeks, 1, 520),
        reply_window_hours=_number(
            table, "reply_window_hours", path, defaults.reply_window_hours, 1, 24 * 365
        ),
        session_gap_hours=_number(
            table, "session_gap_hours", path, defaults.session_gap_hours, 1, 24 * 365
        ),
        ignored_texts=_strings(table, "ignored_texts", path, defaults.ignored_texts),
        duplicates=_choice(table, "duplicates", path, defaults.duplicates),
        count_deleted_as_activity=_boolean(
            table, "count_deleted_as_activity", path, defaults.count_deleted_as_activity
        ),
    )


def _parse_scoring(table: dict[str, Any]) -> ScoringSettings:
    path = "scoring."
    defaults = ScoringSettings()
    known = {name for name in asdict(defaults) if not name.endswith("_threshold")}
    _reject_unknown(table, known | {"thresholds"}, path)

    weights_table = _table(table, "weights", path)
    _reject_unknown(weights_table, set(asdict(defaults.weights)), f"{path}weights.")
    weights = Weights(
        **{
            name: _number(weights_table, name, f"{path}weights.", default, 0, 1)
            for name, default in asdict(defaults.weights).items()
        }
    )
    if sum(asdict(weights).values()) <= 0:
        raise ConfigError("weights_zero", key=f"{path}weights")

    thresholds = _table(table, "thresholds", path)
    _reject_unknown(thresholds, {"high", "medium"}, f"{path}thresholds.")
    high = _number(thresholds, "high", f"{path}thresholds.", defaults.high_threshold, 0, 1)
    medium = _number(thresholds, "medium", f"{path}thresholds.", defaults.medium_threshold, 0, 1)
    if not 0 < medium < high:
        raise ConfigError(
            "threshold_order", key=f"{path}thresholds.high", other=f"{path}thresholds.medium"
        )

    return ScoringSettings(
        weights=weights,
        target_active_weeks=_integer(
            table, "target_active_weeks", path, defaults.target_active_weeks, 1, 520
        ),
        recency_half_life_days=_number(
            table, "recency_half_life_days", path, defaults.recency_half_life_days, 1, 3650
        ),
        min_sessions_for_initiative=_integer(
            table,
            "min_sessions_for_initiative",
            path,
            defaults.min_sessions_for_initiative,
            1,
            1000,
        ),
        min_coverage=_number(table, "min_coverage", path, defaults.min_coverage, 0.01, 1),
        high_threshold=high,
        medium_threshold=medium,
        high_min_inbound_content_messages=_integer(
            table,
            "high_min_inbound_content_messages",
            path,
            defaults.high_min_inbound_content_messages,
            0,
            1_000_000,
        ),
    )


def _reject_unknown(table: dict[str, Any], known: set[str], path: str) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError("unknown_key", key=f"{path}{unknown[0]}")


def _table(data: dict[str, Any], name: str, path: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError("wrong_type", key=f"{path}{name}", expected="table")
    return value


def _reference_date(value: object, key: str) -> date | None:
    # TOML datetimes are `date` subclasses; only a plain date or a string is accepted.
    if isinstance(value, datetime) or not isinstance(value, str | date):
        raise ConfigError("wrong_type", key=key, expected="string")
    if isinstance(value, date):  # TOML allows bare dates: reference_date = 2026-01-31
        return check_reference_date(value, key)
    if value == DATASET_MAX:
        return None
    return parse_reference_date(value, key)


def parse_reference_date(value: str, key: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("invalid_date", key=key) from exc
    return check_reference_date(parsed, key)


def check_reference_date(value: date, key: str) -> date:
    # Far from date.min/date.max, so window arithmetic can never overflow.
    if not MIN_REFERENCE_YEAR <= value.year <= MAX_REFERENCE_YEAR:
        raise ConfigError(
            "out_of_range", key=key, minimum=MIN_REFERENCE_YEAR, maximum=MAX_REFERENCE_YEAR
        )
    return value


def _integer(
    table: dict[str, Any], name: str, path: str, default: int, minimum: int, maximum: int
) -> int:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("wrong_type", key=f"{path}{name}", expected="integer")
    if not minimum <= value <= maximum:
        raise ConfigError("out_of_range", key=f"{path}{name}", minimum=minimum, maximum=maximum)
    return value


def _number(
    table: dict[str, Any],
    name: str,
    path: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError("wrong_type", key=f"{path}{name}", expected="number")
    if not minimum <= value <= maximum:
        raise ConfigError("out_of_range", key=f"{path}{name}", minimum=minimum, maximum=maximum)
    return float(value)  # 168 and 168.0 must hash the same


def _boolean(table: dict[str, Any], name: str, path: str, default: bool) -> bool:
    value = table.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError("wrong_type", key=f"{path}{name}", expected="boolean")
    return value


def _strings(
    table: dict[str, Any], name: str, path: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    value = table.get(name, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("wrong_type", key=f"{path}{name}", expected="list of strings")
    return tuple(value)


def _choice(
    table: dict[str, Any], name: str, path: str, default: DuplicatePolicy
) -> DuplicatePolicy:
    value = table.get(name, default.value)
    try:
        return DuplicatePolicy(value)
    except ValueError as exc:
        raise ConfigError(
            "invalid_choice",
            key=f"{path}{name}",
            choices=", ".join(policy.value for policy in DuplicatePolicy),
        ) from exc
