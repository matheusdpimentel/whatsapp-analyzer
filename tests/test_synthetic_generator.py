import itertools
import re
from datetime import timedelta

import pytest

from whatsapp_analyzer.schema import FILE_MEDIA_KINDS, Direction, MessageType
from whatsapp_analyzer.synthetic.generator import (
    DEFAULT_END,
    MIN_SESSION_GAP,
    PROFILES,
    SUPPORTED_LOCALES,
    GeneratorSettings,
    SyntheticConversation,
    generate,
)

PHONE_LIKE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[\w-]+")
URL = re.compile(r"https?://([^/\s]+)")


def _by_profile(conversations: list[SyntheticConversation], profile: str) -> SyntheticConversation:
    return next(c for c in conversations if c.profile == profile)


@pytest.fixture(scope="module", params=SUPPORTED_LOCALES)
def conversations(request: pytest.FixtureRequest) -> list[SyntheticConversation]:
    return generate(GeneratorSettings(seed=11, conversations=40, locale=request.param))


def test_same_seed_same_output() -> None:
    assert generate(GeneratorSettings(seed=3)) == generate(GeneratorSettings(seed=3))


def test_different_seed_different_output() -> None:
    assert generate(GeneratorSettings(seed=3)) != generate(GeneratorSettings(seed=4))


def test_adding_conversations_keeps_existing_ones() -> None:
    small = generate(GeneratorSettings(conversations=5))[:5]
    large = generate(GeneratorSettings(conversations=9))[:5]
    assert small == large


def test_every_profile_appears_once_enough_conversations_exist() -> None:
    generated = generate(GeneratorSettings(conversations=len(PROFILES)))
    assert {c.profile for c in generated} == {p.name for p in PROFILES} | {
        "edge_cases",
        "ambiguous_dates",
    }


def test_zero_conversations_still_yields_edge_cases() -> None:
    assert [c.profile for c in generate(GeneratorSettings(conversations=0))] == [
        "edge_cases",
        "ambiguous_dates",
    ]


def test_conversation_ids_are_unique_and_sequential(
    conversations: list[SyntheticConversation],
) -> None:
    ids = [c.conversation_id for c in conversations]
    assert ids == [f"chat_{i:04d}" for i in range(1, len(conversations) + 1)]


def test_timelines_are_ordered_and_end_before_the_reference_date(
    conversations: list[SyntheticConversation],
) -> None:
    for conversation in conversations:
        timestamps = [m.timestamp for m in conversation.messages]
        assert timestamps == sorted(timestamps), conversation.conversation_id
        assert timestamps[-1] <= DEFAULT_END, conversation.conversation_id


def test_direction_matches_sender(conversations: list[SyntheticConversation]) -> None:
    for conversation in conversations:
        for message in conversation.messages:
            expected = (
                Direction.OUTBOUND if message.sender == conversation.owner else Direction.INBOUND
            )
            assert message.direction is expected
            assert message.sender in (conversation.owner, *conversation.participants)


def test_one_sided_conversations_have_no_replies(
    conversations: list[SyntheticConversation],
) -> None:
    for conversation in conversations:
        if conversation.profile == "one_sided":
            assert {m.direction for m in conversation.messages} == {Direction.OUTBOUND}


def test_group_conversations_have_several_contacts(
    conversations: list[SyntheticConversation],
) -> None:
    group = _by_profile(conversations, "group")
    assert len(group.participants) == 3


def test_edge_case_conversation_covers_every_message_shape(
    conversations: list[SyntheticConversation],
) -> None:
    edge = _by_profile(conversations, "edge_cases")
    messages = edge.messages
    assert {m.media_kind for m in messages if m.media_kind} == set(FILE_MEDIA_KINDS)
    assert any(m.message_type is MessageType.DELETED for m in messages)
    assert any(m.message_type is MessageType.MEDIA and m.text for m in messages)
    assert any(m.text and "\n" in m.text for m in messages)
    assert any(m.text and len(m.text) > 500 for m in messages)
    assert any(m.text and URL.search(m.text) for m in messages)
    timestamps = [m.timestamp for m in messages]
    assert len(timestamps) != len(set(timestamps)), "expected two messages in the same minute"


def test_ambiguous_dates_conversation_only_uses_days_up_to_twelve(
    conversations: list[SyntheticConversation],
) -> None:
    ambiguous = _by_profile(conversations, "ambiguous_dates")
    assert all(m.timestamp.day <= 12 for m in ambiguous.messages)


def test_contact_details_are_reserved_fictional_values(
    conversations: list[SyntheticConversation],
) -> None:
    texts = [m.text for c in conversations for m in c.messages if m.text]
    for text in texts:
        for phone in PHONE_LIKE.findall(text):
            assert "555-01" in phone, phone
        for match in EMAIL.finditer(text):
            assert match.group().endswith(("example.com", "example.org")), match.group()
        for match in URL.finditer(text):
            assert match.group(1).endswith(("example.com", "example.org")), match.group()


def test_invalid_settings_are_rejected() -> None:
    with pytest.raises(ValueError, match="locale"):
        generate(GeneratorSettings(locale="fr"))
    with pytest.raises(ValueError, match="negative"):
        generate(GeneratorSettings(conversations=-1))


def test_sessions_are_separated_by_the_minimum_gap(
    conversations: list[SyntheticConversation],
) -> None:
    for conversation in conversations:
        if conversation.profile in ("edge_cases", "ambiguous_dates"):
            continue
        timestamps = [m.timestamp for m in conversation.messages]
        gaps = [later - earlier for earlier, later in itertools.pairwise(timestamps)]
        # Gaps are either inside a session (hours at most) or between sessions (>= minimum gap).
        assert all(gap < timedelta(hours=8) or gap >= MIN_SESSION_GAP for gap in gaps)


def test_burst_conversations_are_short(conversations: list[SyntheticConversation]) -> None:
    burst = _by_profile(conversations, "burst")
    duration = burst.messages[-1].timestamp - burst.messages[0].timestamp
    assert len(burst.messages) >= 15
    assert duration < timedelta(hours=6)


def test_continuation_lines_can_look_like_dates(
    conversations: list[SyntheticConversation],
) -> None:
    edge = _by_profile(conversations, "edge_cases")
    continuation_lines = [line for m in edge.messages if m.text for line in m.text.splitlines()[1:]]
    assert any(re.match(r"\d{1,2}/\d{1,2}\b", line) for line in continuation_lines)
