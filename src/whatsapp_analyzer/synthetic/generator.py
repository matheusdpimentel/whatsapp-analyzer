"""Deterministic synthetic conversations.

Every conversation gets its own random generator seeded from `(seed, index)`, so changing the
number of conversations never changes the ones already generated. No wall-clock time is used:
timelines end at `GeneratorSettings.end`.
"""

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from whatsapp_analyzer.schema import Direction, MediaKind, MessageType
from whatsapp_analyzer.synthetic.phrases import PHRASES, PhraseBank

GENERATOR_VERSION = 1  # bump when the output for a given seed changes on purpose
DEFAULT_END = datetime(2026, 1, 31, 18, 0, 0)
SUPPORTED_LOCALES = tuple(PHRASES)

OWNER_REPLY_PROBABILITY = 0.9
MEDIA_PROBABILITY = 0.07
ACKNOWLEDGEMENT_PROBABILITY = 0.25
REPLY_DELAY_MINUTES = (1, 2, 4, 9, 25, 70, 180, 420)
BURST_REPLY_DELAY_MINUTES = (1, 1, 2, 3, 5)
# Longer than the 24-hour session gap the metrics use by default, so sessions stay distinct.
MIN_SESSION_GAP = timedelta(hours=30)


@dataclass(frozen=True)
class SyntheticMessage:
    timestamp: datetime
    sender: str
    direction: Direction
    message_type: MessageType
    text: str | None  # caption for media; None for media without caption and deleted messages
    media_kind: MediaKind | None = None


@dataclass(frozen=True)
class SyntheticConversation:
    conversation_id: str
    title: str
    profile: str
    owner: str
    participants: tuple[str, ...]
    messages: tuple[SyntheticMessage, ...]


@dataclass(frozen=True)
class Profile:
    """Shape of a conversation; profiles cover the engagement patterns the metrics must handle."""

    name: str
    weight: float
    sessions: tuple[int, int]
    turns: tuple[int, int]
    contact_starts: float
    contact_replies: float
    span_days: int
    last_active_days_ago: tuple[int, int]
    participants: int = 1
    reply_delays: tuple[int, ...] = REPLY_DELAY_MINUTES


PROFILES = (
    Profile("engaged", 3, (6, 12), (2, 6), 0.5, 0.95, 240, (0, 20)),
    Profile("responsive", 3, (3, 7), (2, 4), 0.1, 0.8, 180, (0, 60)),
    Profile("one_sided", 2, (2, 5), (1, 1), 0.0, 0.0, 150, (0, 90)),
    Profile("dormant", 2, (2, 5), (2, 4), 0.3, 0.7, 120, (200, 400)),
    Profile("new_contact", 2, (1, 1), (2, 4), 0.5, 1.0, 0, (0, 14)),
    Profile(
        "burst",
        1,
        (1, 1),
        (15, 30),
        0.5,
        0.97,
        0,
        (0, 30),
        reply_delays=BURST_REPLY_DELAY_MINUTES,
    ),
    Profile("group", 1, (3, 6), (2, 6), 0.6, 0.9, 90, (0, 45), participants=3),
)


@dataclass(frozen=True)
class GeneratorSettings:
    seed: int = 7
    conversations: int = 10
    locale: str = "en"
    end: datetime = DEFAULT_END


def generate(settings: GeneratorSettings) -> list[SyntheticConversation]:
    """Generate `settings.conversations` profile-based conversations plus two fixed edge cases.

    With at least `len(PROFILES)` conversations, every profile appears at least once.
    """
    if settings.locale not in PHRASES:
        raise ValueError(f"unsupported locale: {settings.locale!r}")
    if settings.conversations < 0:
        raise ValueError("conversations must not be negative")
    bank = PHRASES[settings.locale]
    conversations = [
        _profile_conversation(settings, bank, index) for index in range(settings.conversations)
    ]
    first_edge_index = settings.conversations + 1
    conversations.append(_edge_case_conversation(bank, settings.end, first_edge_index))
    conversations.append(_ambiguous_dates_conversation(bank, settings.end, first_edge_index + 1))
    return conversations


def _conversation_id(index: int) -> str:
    return f"chat_{index:04d}"


def _profile_conversation(
    settings: GeneratorSettings, bank: PhraseBank, index: int
) -> SyntheticConversation:
    rng = random.Random(f"{settings.seed}:{index}")
    # The first conversations cycle through every profile so small datasets still cover them all.
    if index < len(PROFILES):
        profile = PROFILES[index]
    else:
        profile = rng.choices(PROFILES, weights=[p.weight for p in PROFILES])[0]
    names = rng.sample(bank.contact_names, profile.participants)
    title = names[0] if profile.participants == 1 else f"{bank.owner_name} group {index + 1}"

    last_active = settings.end - timedelta(
        days=rng.randint(*profile.last_active_days_ago), hours=12, minutes=rng.randint(0, 600)
    )
    first_active = last_active - timedelta(days=profile.span_days)
    span_seconds = int((last_active - first_active).total_seconds())
    starts = sorted(
        first_active + timedelta(seconds=rng.randint(0, span_seconds))
        for _ in range(rng.randint(*profile.sessions) - 1)
    )
    starts.append(last_active)

    messages: list[SyntheticMessage] = []
    for start in starts:
        cursor = max(start, messages[-1].timestamp + MIN_SESSION_GAP) if messages else start
        messages.extend(_session(rng, profile, bank, names, cursor))

    # Long sessions can run past the end of the timeline; shift them back, keeping their shape.
    overflow = messages[-1].timestamp - settings.end
    if overflow > timedelta(0):
        messages = [replace(m, timestamp=m.timestamp - overflow) for m in messages]
    return SyntheticConversation(
        conversation_id=_conversation_id(index + 1),
        title=title,
        profile=profile.name,
        owner=bank.owner_name,
        participants=tuple(names),
        messages=tuple(messages),
    )


def _session(
    rng: random.Random,
    profile: Profile,
    bank: PhraseBank,
    contacts: list[str],
    start: datetime,
) -> list[SyntheticMessage]:
    owner_speaks = rng.random() >= profile.contact_starts
    timestamp = start
    messages: list[SyntheticMessage] = []
    for turn in range(rng.randint(*profile.turns)):
        if turn > 0:
            reply_probability = OWNER_REPLY_PROBABILITY if owner_speaks else profile.contact_replies
            if rng.random() >= reply_probability:
                break
            timestamp += timedelta(minutes=rng.choice(profile.reply_delays))
        sender = bank.owner_name if owner_speaks else rng.choice(contacts)
        for position in range(rng.randint(1, 3)):
            if position > 0:
                timestamp += timedelta(seconds=rng.randint(5, 120))
            opener = turn == 0 and position == 0
            messages.append(_message(rng, bank, timestamp, sender, opener))
        owner_speaks = not owner_speaks
    return messages


def _message(
    rng: random.Random, bank: PhraseBank, timestamp: datetime, sender: str, opener: bool
) -> SyntheticMessage:
    from_owner = sender == bank.owner_name
    direction = Direction.OUTBOUND if from_owner else Direction.INBOUND
    roll = rng.random()
    if roll < MEDIA_PROBABILITY:
        kind = rng.choice(list(MediaKind))
        caption = (
            rng.choice(bank.captions) if kind is MediaKind.IMAGE and rng.random() < 0.5 else None
        )
        return SyntheticMessage(timestamp, sender, direction, MessageType.MEDIA, caption, kind)
    if opener:
        pool = bank.owner_openers if from_owner else bank.contact_openers
    elif roll < MEDIA_PROBABILITY + ACKNOWLEDGEMENT_PROBABILITY:
        pool = bank.acknowledgements
    else:
        pool = bank.owner_replies if from_owner else bank.contact_replies
    return SyntheticMessage(timestamp, sender, direction, MessageType.TEXT, rng.choice(pool))


def _edge_case_conversation(bank: PhraseBank, end: datetime, index: int) -> SyntheticConversation:
    """One conversation containing every message shape the parser and metrics must handle."""
    owner = bank.owner_name
    contact = bank.contact_names[-1]
    start = end - timedelta(days=3)
    long_text = " ".join(bank.contact_replies * 4)

    def at(minutes: int) -> datetime:
        return start + timedelta(minutes=minutes)

    def text(minutes: int, sender: str, body: str) -> SyntheticMessage:
        direction = Direction.OUTBOUND if sender == owner else Direction.INBOUND
        return SyntheticMessage(at(minutes), sender, direction, MessageType.TEXT, body)

    def media(minutes: int, kind: MediaKind, caption: str | None = None) -> SyntheticMessage:
        return SyntheticMessage(
            at(minutes), contact, Direction.INBOUND, MessageType.MEDIA, caption, kind
        )

    messages = (
        text(0, contact, bank.contact_openers[0]),
        text(1, contact, bank.auto_reply),
        text(3, owner, bank.owner_replies[1]),
        text(5, contact, bank.multiline),
        text(9, owner, bank.with_link),
        text(9, owner, bank.owner_replies[5]),  # same minute as the previous message
        text(12, contact, bank.with_contact_details),
        media(14, MediaKind.IMAGE),
        media(15, MediaKind.IMAGE, bank.captions[0]),
        media(16, MediaKind.AUDIO),
        media(17, MediaKind.VIDEO),
        media(18, MediaKind.STICKER),
        media(19, MediaKind.DOCUMENT),
        SyntheticMessage(at(21), contact, Direction.INBOUND, MessageType.DELETED, None),
        text(23, contact, bank.acknowledgements[5]),
        text(24, contact, long_text),
        text(30, owner, bank.acknowledgements[0]),
    )
    return SyntheticConversation(
        conversation_id=_conversation_id(index),
        title=contact,
        profile="edge_cases",
        owner=owner,
        participants=(contact,),
        messages=messages,
    )


def _ambiguous_dates_conversation(
    bank: PhraseBank, end: datetime, index: int
) -> SyntheticConversation:
    """Every day is 12 or lower, so day/month order cannot be inferred from this file alone."""
    owner = bank.owner_name
    contact = bank.contact_names[-2]
    month_start = datetime(end.year - 1, 11, 1, 9, 30)
    dates = [month_start + timedelta(days=day) for day in (1, 4, 10)]
    messages = (
        SyntheticMessage(
            dates[0], owner, Direction.OUTBOUND, MessageType.TEXT, bank.owner_openers[0]
        ),
        SyntheticMessage(
            dates[1], contact, Direction.INBOUND, MessageType.TEXT, bank.contact_replies[0]
        ),
        SyntheticMessage(
            dates[2], owner, Direction.OUTBOUND, MessageType.TEXT, bank.owner_replies[0]
        ),
    )
    return SyntheticConversation(
        conversation_id=_conversation_id(index),
        title=contact,
        profile="ambiguous_dates",
        owner=owner,
        participants=(contact,),
        messages=messages,
    )
