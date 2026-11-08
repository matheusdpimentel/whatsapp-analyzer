"""Normalization, weighted score and level. Pure functions over stored raw metrics.

    frequency   = min(active_weeks / target_active_weeks, 1)
    reciprocity = answered_outbound_turns / (outbound_turns - pending_outbound_turns)
                  (empty when that denominator is 0)
    initiative  = contact_started_sessions / sessions
                  (empty when sessions < min_sessions_for_initiative)
    recency     = 0.5 ** (days_since_last_inbound / recency_half_life_days)
                  (empty when there is no inbound message)

    score    = sum(w_k * x_k) / sum(w_k) over the components that are not empty
    coverage = sum(w_k of non-empty components) / sum(all w_k)

Levels: groups are NOT_COMPARABLE; conversations without considered messages or with coverage
below `min_coverage` are INSUFFICIENT_DATA; otherwise HIGH (score >= high threshold and at least
`high_min_inbound_content_messages` inbound text/media messages), MEDIUM, or LOW. Components are
rounded to 6 decimals and the score to 4 before comparing, so results do not depend on
floating-point noise.

The level describes observable activity only. It is not a judgement about the person.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum

from whatsapp_analyzer.config import ScoringSettings
from whatsapp_analyzer.metrics.raw import RawMetrics

COMPONENT_DIGITS = 6
SCORE_DIGITS = 4


class Level(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class Reason(StrEnum):
    SCORED = "scored"
    HIGH_GATE = "high_gate"  # score reached HIGH but too few inbound content messages
    LOW_COVERAGE = "low_coverage"
    NO_MESSAGES = "no_messages"
    GROUP = "group_conversation"


@dataclass(frozen=True)
class Score:
    frequency: float | None
    reciprocity: float | None
    initiative: float | None
    recency: float | None
    coverage: float | None
    score: float | None
    level: Level
    reason: Reason


def score_conversation(raw: RawMetrics, settings: ScoringSettings) -> Score:
    if raw.is_group:
        return Score(None, None, None, None, None, None, Level.NOT_COMPARABLE, Reason.GROUP)
    if raw.messages_considered == 0:
        return Score(
            None, None, None, None, None, None, Level.INSUFFICIENT_DATA, Reason.NO_MESSAGES
        )

    components = normalize(raw, settings)
    weights = asdict(settings.weights)
    present = {name: value for name, value in components.items() if value is not None}
    covered = sum(weights[name] for name in present)
    coverage = round(covered / sum(weights.values()), COMPONENT_DIGITS)
    if not present or covered <= 0 or coverage < settings.min_coverage:
        return Score(
            **components,
            coverage=coverage,
            score=None,
            level=Level.INSUFFICIENT_DATA,
            reason=Reason.LOW_COVERAGE,
        )

    value = round(sum(weights[name] * x for name, x in present.items()) / covered, SCORE_DIGITS)
    if value >= settings.high_threshold:
        if raw.inbound_content_messages >= settings.high_min_inbound_content_messages:
            level, reason = Level.HIGH, Reason.SCORED
        else:
            level, reason = Level.MEDIUM, Reason.HIGH_GATE
    elif value >= settings.medium_threshold:
        level, reason = Level.MEDIUM, Reason.SCORED
    else:
        level, reason = Level.LOW, Reason.SCORED
    return Score(**components, coverage=coverage, score=value, level=level, reason=reason)


def normalize(raw: RawMetrics, settings: ScoringSettings) -> dict[str, float | None]:
    frequency = min(raw.active_weeks / settings.target_active_weeks, 1.0)

    reciprocity = None
    if raw.outbound_turns is not None and raw.answered_outbound_turns is not None:
        decided = raw.outbound_turns - (raw.pending_outbound_turns or 0)
        if decided > 0:
            reciprocity = raw.answered_outbound_turns / decided

    initiative = None
    if (
        raw.contact_started_sessions is not None
        and raw.sessions >= settings.min_sessions_for_initiative
    ):
        initiative = raw.contact_started_sessions / raw.sessions

    recency = None
    if raw.days_since_last_inbound is not None:
        recency = 0.5 ** (raw.days_since_last_inbound / settings.recency_half_life_days)

    return {
        "frequency": _round(frequency),
        "reciprocity": _round(reciprocity),
        "initiative": _round(initiative),
        "recency": _round(recency),
    }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, COMPONENT_DIGITS)
