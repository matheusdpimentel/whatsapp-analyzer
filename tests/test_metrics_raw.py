"""Raw metric formulas, checked against values worked out by hand."""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from whatsapp_analyzer.config import DuplicatePolicy, MetricsSettings
from whatsapp_analyzer.metrics.raw import RawMetrics, StoredMessage, compute_raw_metrics
from whatsapp_analyzer.schema import Direction, MediaKind, MessageType

REFERENCE = date(2026, 1, 31)
SETTINGS = MetricsSettings()
IN, OUT = Direction.INBOUND, Direction.OUTBOUND


def msg(
    seq: int,
    when: str,
    direction: Direction | None,
    text: str = "hello",
    message_type: MessageType = MessageType.TEXT,
    sender: str | None = None,
    media_kind: MediaKind | None = None,
) -> StoredMessage:
    return StoredMessage(
        seq=seq,
        timestamp=datetime.fromisoformat(when),
        sender=sender if sender is not None else ("contact" if direction is IN else "owner"),
        direction=direction,
        text=text,
        message_type=message_type,
        media_kind=media_kind,
    )


def raw(
    messages: list[StoredMessage],
    settings: MetricsSettings = SETTINGS,
    is_group: bool = False,
    reference: date = REFERENCE,
) -> RawMetrics:
    return compute_raw_metrics(messages, settings, reference, is_group)


def test_simple_exchange() -> None:
    result = raw(
        [
            msg(0, "2026-01-10T10:00:00", OUT),
            msg(1, "2026-01-10T10:05:00", IN),
            msg(2, "2026-01-20T09:00:00", IN),
            msg(3, "2026-01-20T09:01:00", OUT),
        ]
    )
    assert result.messages_considered == 4
    assert (result.inbound_messages, result.outbound_messages) == (2, 2)
    # 2026-01-10 is ISO week 2, 2026-01-20 is ISO week 4.
    assert result.active_weeks == 2
    # Turns: OUT, IN, OUT. The first is answered; the last has no reply and was sent 11 days
    # before the end of the reference date, beyond the 7-day window: unanswered, not pending.
    assert (result.outbound_turns, result.answered_outbound_turns) == (2, 1)
    assert result.pending_outbound_turns == 0
    # Sessions split by gaps of 24 h or more: [10th], [20th]; the second starts inbound.
    assert (result.sessions, result.contact_started_sessions) == (2, 1)
    assert result.last_inbound_at == datetime(2026, 1, 20, 9, 0)
    assert result.days_since_last_inbound == 11


def test_conversation_without_outbound_messages() -> None:
    result = raw([msg(0, "2026-01-30T08:00:00", IN), msg(1, "2026-01-30T08:01:00", IN)])
    assert result.outbound_turns == 0
    assert result.answered_outbound_turns == 0
    assert result.sessions == 1 and result.contact_started_sessions == 1
    assert result.days_since_last_inbound == 1


def test_conversation_without_inbound_messages() -> None:
    result = raw([msg(0, "2026-01-01T08:00:00", OUT), msg(1, "2026-01-15T08:00:00", OUT)])
    assert result.inbound_messages == 0
    assert result.active_weeks == 0
    assert result.last_inbound_at is None and result.days_since_last_inbound is None
    assert (result.outbound_turns, result.answered_outbound_turns) == (1, 0)
    assert result.contact_started_sessions == 0


def test_only_system_messages_are_not_considered() -> None:
    result = raw(
        [
            msg(0, "2026-01-01T08:00:00", None, "joined", MessageType.SYSTEM, sender=""),
            msg(1, "2026-01-02T08:00:00", None, "left", MessageType.SYSTEM, sender=""),
        ]
    )
    assert result.messages_considered == 0
    assert result.excluded_system == 2
    assert result.sessions == 0


def test_reply_outside_the_window_is_not_an_answer() -> None:
    settings = replace(SETTINGS, reply_window_hours=48.0)
    result = raw(
        [
            msg(0, "2026-01-01T10:00:00", OUT),
            msg(1, "2026-01-03T10:00:00", IN),  # exactly 48 h later: answered
            msg(2, "2026-01-05T10:00:00", OUT),
            msg(3, "2026-01-07T10:00:01", IN),  # one second too late
        ],
        settings,
    )
    assert (result.outbound_turns, result.answered_outbound_turns) == (2, 1)


def test_recent_unanswered_turn_is_pending() -> None:
    result = raw([msg(0, "2026-01-20T10:00:00", IN), msg(1, "2026-01-30T10:00:00", OUT)])
    # The window (7 days) after the last outbound message has not closed by 2026-02-01 00:00.
    assert (result.outbound_turns, result.answered_outbound_turns) == (1, 0)
    assert result.pending_outbound_turns == 1


def test_burst_of_messages_is_one_turn_and_one_session() -> None:
    burst = [msg(i, f"2026-01-10T10:0{i}:00", OUT) for i in range(5)]
    result = raw([*burst, msg(5, "2026-01-10T10:30:00", IN)])
    assert (result.outbound_turns, result.answered_outbound_turns) == (1, 1)
    assert result.sessions == 1 and result.contact_started_sessions == 0


def test_messages_after_the_reference_date_are_excluded() -> None:
    result = raw(
        [
            msg(0, "2026-01-31T23:59:59", IN),  # last second of the reference date: kept
            msg(1, "2026-02-01T00:00:00", OUT),
            msg(2, "2026-02-03T00:00:00", IN),
        ]
    )
    assert result.messages_considered == 1
    assert result.excluded_after_reference == 2
    assert result.days_since_last_inbound == 0


def test_active_weeks_window_boundaries() -> None:
    settings = replace(SETTINGS, lookback_weeks=2)
    # The window is the 14 days ending on the reference date: 2026-01-18 .. 2026-01-31.
    result = raw(
        [
            msg(0, "2026-01-17T12:00:00", IN),  # outside
            msg(1, "2026-01-18T00:00:00", IN),  # first day inside (ISO week 3)
            msg(2, "2026-01-19T12:00:00", IN),  # ISO week 4
            msg(3, "2026-01-30T12:00:00", IN),  # ISO week 5
        ],
        settings,
    )
    assert result.active_weeks == 3


def test_iso_weeks_across_the_new_year() -> None:
    # 2025-12-29 and 2026-01-04 are both in ISO week 1 of 2026.
    result = raw([msg(0, "2025-12-29T09:00:00", IN), msg(1, "2026-01-04T09:00:00", IN)])
    assert result.active_weeks == 1


def test_timestamp_ties_follow_source_order() -> None:
    # Same minute, alternating directions: the canonical sequence is (timestamp, seq).
    tied = [
        msg(0, "2026-01-10T10:00:00", OUT, "a"),
        msg(1, "2026-01-10T10:00:00", IN, "b"),
        msg(2, "2026-01-10T10:00:00", OUT, "c"),
    ]
    result = raw(tied)
    assert (result.outbound_turns, result.answered_outbound_turns) == (2, 1)
    # Input order does not matter: the same seq values give the same sequence.
    assert raw(list(reversed(tied))) == result
    # Different source order (seq) is different content and may give different turns.
    swapped = [replace(tied[0], seq=1), replace(tied[1], seq=0), tied[2]]
    assert raw(swapped).outbound_turns == 1
    assert raw(swapped).contact_started_sessions == 1


def test_duplicates_are_not_invariant_and_collapse_is_explicit() -> None:
    messages = [
        msg(0, "2026-01-10T10:00:00", IN, "ok"),
        msg(1, "2026-01-10T10:00:00", OUT, "hi"),
        msg(2, "2026-01-10T10:00:00", IN, "ok"),  # exact repeat of seq 0
    ]
    kept = raw(messages)
    collapsed = raw(messages, replace(SETTINGS, duplicates=DuplicatePolicy.COLLAPSE))
    # Keeping the repeat creates an inbound turn after the outbound one: it counts as answered.
    assert (kept.outbound_turns, kept.answered_outbound_turns) == (1, 1)
    assert kept.inbound_messages == 2 and kept.collapsed_duplicates == 0
    # Collapsing removes it, and the outbound turn becomes pending (no reply yet).
    assert (collapsed.outbound_turns, collapsed.answered_outbound_turns) == (1, 0)
    assert collapsed.inbound_messages == 1 and collapsed.collapsed_duplicates == 1


def test_collapse_keeps_messages_that_differ_in_any_field() -> None:
    settings = replace(SETTINGS, duplicates=DuplicatePolicy.COLLAPSE)
    result = raw(
        [
            msg(0, "2026-01-10T10:00:00", IN, "ok"),
            msg(1, "2026-01-10T10:00:00", IN, "ok", sender="someone else"),
            msg(2, "2026-01-10T10:00:01", IN, "ok"),
        ],
        settings,
    )
    assert result.collapsed_duplicates == 0 and result.inbound_messages == 3


def test_short_replies_and_media_count_like_any_message() -> None:
    result = raw(
        [
            msg(0, "2026-01-10T10:00:00", OUT, "Can you confirm?"),
            msg(1, "2026-01-10T10:01:00", IN, "ok"),
            msg(2, "2026-01-12T10:00:00", OUT, "Thanks"),
            msg(3, "2026-01-12T10:01:00", IN, "", MessageType.MEDIA, media_kind=MediaKind.STICKER),
        ]
    )
    assert result.answered_outbound_turns == 2
    assert result.inbound_content_messages == 2


def test_deleted_messages_count_as_activity_but_not_as_content() -> None:
    messages = [
        msg(0, "2026-01-10T10:00:00", OUT),
        msg(1, "2026-01-10T10:01:00", IN, "", MessageType.DELETED),
    ]
    counted = raw(messages)
    assert counted.answered_outbound_turns == 1
    assert counted.inbound_messages == 1
    assert counted.inbound_content_messages == 0 and counted.inbound_deleted_messages == 1

    excluded = raw(messages, replace(SETTINGS, count_deleted_as_activity=False))
    assert excluded.inbound_messages == 0 and excluded.excluded_deleted == 1
    assert excluded.answered_outbound_turns == 0


def test_ignored_texts_are_exact_matches() -> None:
    result = raw(
        [
            msg(0, "2026-01-10T10:00:00", IN, "(Waiting for this message)"),
            msg(1, "2026-01-10T10:01:00", IN, "  (Waiting for this message)  "),
            msg(2, "2026-01-10T10:02:00", IN, "(Waiting for this message) really"),
        ]
    )
    assert result.excluded_ignored_text == 2
    assert result.inbound_messages == 1


def test_groups_get_descriptive_values_only() -> None:
    result = raw(
        [
            msg(0, "2026-01-10T10:00:00", IN, sender="first"),
            msg(1, "2026-01-10T10:01:00", IN, sender="second"),
            msg(2, "2026-01-10T10:02:00", OUT),
        ],
        is_group=True,
    )
    assert result.is_group
    assert result.inbound_messages == 2 and result.active_weeks == 1 and result.sessions == 1
    assert result.outbound_turns is None and result.answered_outbound_turns is None
    assert result.pending_outbound_turns is None and result.contact_started_sessions is None
    assert result.days_since_last_inbound == 21


def test_session_gap_is_inclusive() -> None:
    result = raw(
        [
            msg(0, "2026-01-10T10:00:00", OUT),
            msg(1, "2026-01-11T10:00:00", IN),  # exactly 24 h: a new session
            msg(2, "2026-01-12T09:59:59", OUT),  # just under 24 h: same session
        ]
    )
    assert result.sessions == 2 and result.contact_started_sessions == 1


def test_no_clock_dependency() -> None:
    messages = [msg(0, "2020-01-01T10:00:00", IN)]
    assert raw(messages, reference=date(2020, 1, 1)).days_since_last_inbound == 0
    assert (
        raw(messages, reference=date(2020, 1, 1) + timedelta(days=90)).days_since_last_inbound == 90
    )


@pytest.mark.parametrize("policy", list(DuplicatePolicy))
def test_counters_add_up(policy: DuplicatePolicy) -> None:
    messages = [
        msg(0, "2026-01-01T10:00:00", None, "x", MessageType.SYSTEM, sender=""),
        msg(1, "2026-01-02T10:00:00", IN, "(Waiting for this message)"),
        msg(2, "2026-01-03T10:00:00", IN, "", MessageType.DELETED),
        msg(3, "2026-01-04T10:00:00", IN, "a"),
        msg(4, "2026-01-04T10:00:00", IN, "a"),
        msg(5, "2026-03-01T10:00:00", OUT, "later"),
    ]
    settings = replace(SETTINGS, duplicates=policy, count_deleted_as_activity=False)
    result = raw(messages, settings)
    excluded = (
        result.excluded_system
        + result.excluded_after_reference
        + result.excluded_ignored_text
        + result.excluded_deleted
        + result.collapsed_duplicates
    )
    assert result.messages_considered + excluded == len(messages)


def test_pending_window_closing_exactly_at_the_end_of_the_reference_date() -> None:
    # 2026-01-25 00:00 + 168 h = 2026-02-01 00:00 = end of the reference date. A reply at that
    # instant would still count as answered, so the turn is pending, not unanswered.
    result = raw([msg(0, "2026-01-20T10:00:00", IN), msg(1, "2026-01-25T00:00:00", OUT)])
    assert result.pending_outbound_turns == 1
    closed = raw([msg(0, "2026-01-20T10:00:00", IN), msg(1, "2026-01-24T23:59:59", OUT)])
    assert closed.pending_outbound_turns == 0
