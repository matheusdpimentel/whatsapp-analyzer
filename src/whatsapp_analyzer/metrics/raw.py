"""Raw metrics of one conversation version. Pure functions: no database, no clock.

Canonical sequence
    Messages are ordered by (timestamp, seq), where `seq` is the position in the source export.
    Ties between equal timestamps are therefore resolved by source order, which is part of the
    content hash, so the same content always yields the same sequence.

Considered messages
    Every message except, in this order of checks: system messages; messages after the end of
    the reference date; text messages whose whole text is in `ignored_texts`; deleted messages
    when `count_deleted_as_activity` is false; and, with `duplicates = "collapse"`, exact repeats
    of an earlier message in the canonical sequence. Each excluded message is counted in exactly
    one `excluded_*`/`collapsed_duplicates` counter. Message length is never used.

Groups
    Group chats get descriptive values only (counts, active weeks, sessions, recency). Turn-based
    reciprocity and initiative have no comparable meaning when several people talk, so they are
    left empty (NULL) instead of being computed.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from whatsapp_analyzer.config import DuplicatePolicy, MetricsSettings
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType


@dataclass(frozen=True)
class StoredMessage:
    seq: int
    timestamp: datetime
    sender: str
    direction: Direction | None
    text: str
    message_type: MessageType
    media_kind: MediaKind | None


@dataclass(frozen=True)
class RawMetrics:
    """Field names match the `conversation_metrics` columns."""

    is_group: bool
    messages_considered: int
    inbound_messages: int
    outbound_messages: int
    inbound_content_messages: int  # inbound text or media (not deleted)
    inbound_deleted_messages: int
    excluded_system: int
    excluded_after_reference: int
    excluded_ignored_text: int
    excluded_deleted: int
    collapsed_duplicates: int
    active_weeks: int
    outbound_turns: int | None
    answered_outbound_turns: int | None
    pending_outbound_turns: int | None
    sessions: int
    contact_started_sessions: int | None
    last_inbound_at: datetime | None
    days_since_last_inbound: int | None


@dataclass(frozen=True)
class _Window:
    reference_date: date
    end: datetime  # exclusive: midnight after the reference date
    first_active_day: date
    reply_window: timedelta
    session_gap: timedelta


def compute_raw_metrics(
    messages: Iterable[StoredMessage],
    settings: MetricsSettings,
    reference_date: date,
    is_group: bool,
) -> RawMetrics:
    window = _Window(
        reference_date=reference_date,
        end=datetime.combine(reference_date + timedelta(days=1), time()),
        first_active_day=reference_date - timedelta(weeks=settings.lookback_weeks, days=-1),
        reply_window=timedelta(hours=settings.reply_window_hours),
        session_gap=timedelta(hours=settings.session_gap_hours),
    )
    ordered = sorted(messages, key=lambda m: (m.timestamp, m.seq))
    considered, excluded = _select(ordered, settings, window)

    inbound = [m for m in considered if m.direction is Direction.INBOUND]
    last_inbound = max((m.timestamp for m in inbound), default=None)
    turns = None if is_group else _turns(considered, window)
    sessions = _sessions(considered, window)

    return RawMetrics(
        is_group=is_group,
        messages_considered=len(considered),
        inbound_messages=len(inbound),
        outbound_messages=len(considered) - len(inbound),
        inbound_content_messages=sum(m.message_type is not MessageType.DELETED for m in inbound),
        inbound_deleted_messages=sum(m.message_type is MessageType.DELETED for m in inbound),
        excluded_system=excluded["system"],
        excluded_after_reference=excluded["after_reference"],
        excluded_ignored_text=excluded["ignored_text"],
        excluded_deleted=excluded["deleted"],
        collapsed_duplicates=excluded["duplicate"],
        active_weeks=len(
            {
                m.timestamp.isocalendar()[:2]
                for m in inbound
                if m.timestamp.date() >= window.first_active_day
            }
        ),
        outbound_turns=turns[0] if turns else None,
        answered_outbound_turns=turns[1] if turns else None,
        pending_outbound_turns=turns[2] if turns else None,
        sessions=len(sessions),
        contact_started_sessions=(
            None
            if is_group
            else sum(session[0].direction is Direction.INBOUND for session in sessions)
        ),
        last_inbound_at=last_inbound,
        days_since_last_inbound=(
            (reference_date - last_inbound.date()).days if last_inbound else None
        ),
    )


def _select(
    ordered: list[StoredMessage], settings: MetricsSettings, window: _Window
) -> tuple[list[StoredMessage], dict[str, int]]:
    ignored = {text.strip() for text in settings.ignored_texts}
    excluded = dict.fromkeys(
        ("system", "after_reference", "ignored_text", "deleted", "duplicate"), 0
    )
    seen: set[tuple[object, ...]] = set()
    considered: list[StoredMessage] = []
    for message in ordered:
        if message.message_type is MessageType.SYSTEM or message.direction is None:
            excluded["system"] += 1
        elif message.timestamp >= window.end:
            excluded["after_reference"] += 1
        elif message.message_type is MessageType.TEXT and message.text.strip() in ignored:
            excluded["ignored_text"] += 1
        elif message.message_type is MessageType.DELETED and not settings.count_deleted_as_activity:
            excluded["deleted"] += 1
        elif settings.duplicates is DuplicatePolicy.COLLAPSE and _seen_before(message, seen):
            excluded["duplicate"] += 1
        else:
            considered.append(message)
    return considered, excluded


def _seen_before(message: StoredMessage, seen: set[tuple[object, ...]]) -> bool:
    key = (
        message.timestamp,
        message.sender,
        message.direction,
        message.text,
        message.message_type,
        message.media_kind,
    )
    if key in seen:
        return True
    seen.add(key)
    return False


def _turns(considered: list[StoredMessage], window: _Window) -> tuple[int, int, int]:
    """Outbound turns, answered ones and pending ones.

    A turn is a maximal run of consecutive messages in one direction. An outbound turn is
    answered when the next turn is inbound and starts within the reply window after the turn's
    last message. An unanswered outbound turn whose window has not closed by the end of the
    reference date is pending: it is neither answered nor unanswered yet.
    """
    turns: list[tuple[Direction | None, datetime, datetime]] = []  # direction, first, last
    for message in considered:
        if turns and turns[-1][0] is message.direction:
            turns[-1] = (turns[-1][0], turns[-1][1], message.timestamp)
        else:
            turns.append((message.direction, message.timestamp, message.timestamp))

    outbound = answered = pending = 0
    for index, (direction, _, last) in enumerate(turns):
        if direction is not Direction.OUTBOUND:
            continue
        outbound += 1
        following = turns[index + 1] if index + 1 < len(turns) else None
        if following is not None and following[1] - last <= window.reply_window:
            answered += 1  # the next turn is necessarily inbound
        elif following is None and window.end - last <= window.reply_window:
            pending += 1
    return outbound, answered, pending


def _sessions(considered: list[StoredMessage], window: _Window) -> list[list[StoredMessage]]:
    sessions: list[list[StoredMessage]] = []
    for message in considered:
        if sessions and message.timestamp - sessions[-1][-1].timestamp < window.session_gap:
            sessions[-1].append(message)
        else:
            sessions.append([message])
    return sessions
