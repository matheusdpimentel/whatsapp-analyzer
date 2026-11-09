"""Masking of common identifiers before text is shown to a model.

This is PSEUDONYMIZATION of frequent patterns, not anonymization. It replaces e-mail
addresses, URLs, CPF/CNPJ-shaped numbers, phone numbers, other long numbers, @mentions and the
known names of the contact and the owner. Names of other people, addresses, rare identifiers
and the context of the conversation itself remain, and the masked text still leaves the
machine when it is sent to a provider.

Placeholders are numbered per distinct value within one conversation (`[PHONE_1]`,
`[PHONE_2]`), so the model can tell values apart without seeing them. The mapping back to the
original values stays in memory and is never sent, printed or stored.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from enum import StrEnum

MASKING_VERSION = 1


class MaskKind(StrEnum):
    EMAIL = "EMAIL"
    URL = "URL"
    ID_NUMBER = "ID_NUMBER"  # CPF/CNPJ-shaped
    PHONE = "PHONE"
    NUMBER = "NUMBER"  # any other run of 6+ digits
    MENTION = "MENTION"
    CONTACT_NAME = "CONTACT_NAME"
    OWNER_NAME = "OWNER_NAME"


# Every pattern starts only at a token boundary (the lookbehinds), so scanning stays linear even
# for pathological input such as "a.a.a.a..." or long runs of digits and dashes. Patterns run
# on raw text; placeholders are inserted as private-use sentinels (see `Masker`).
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TLDS = frozenset(
    [
        "com",
        "net",
        "org",
        "br",
        "io",
        "me",
        "ly",
        "app",
        "dev",
        "info",
        "gov",
        "edu",
        "co",
        "gl",
        "ee",
        "xyz",
        "online",
        "shop",
        "site",
        "store",
        "tv",
        "ai",
        "link",
        "us",
        "uk",
        "pt",
        "es",
        "fr",
        "de",
        "it",
        "ar",
        "mx",
        "cl",
        "biz",
        "club",
        "top",
        "live",
        "blog",
        "page",
        "tech",
        "cc",
        "to",
        "gg",
        "so",
        "sh",
    ]
)
# Boundaries use letters and digits only (`[^\W_]`), not `\w`: WhatsApp formats italics as
# _text_, and an underscore must not hide what it surrounds.
_ALNUM_BEFORE = r"(?<![^\W_])"
_ALNUM_AFTER = r"(?![^\W_])"
_SENTINELS = "\ue000-\ue01f"  # private-use range reserved for placeholders
# Sentinels are allowed inside a URL: a URL that contains an already masked e-mail address
# (in a query, a path or userinfo) is replaced whole, together with that placeholder.
_URL_CHARS = r"[^\s<>\"']"
_SCHEME_URL = re.compile(rf"(?i){_ALNUM_BEFORE}(?:https?://|www\.){_URL_CHARS}+")
# A whole host name (labels joined by dots), optionally with a path, query or fragment. The
# callback decides whether it is a domain, so a host is masked whole or not at all.
_HOST = re.compile(
    rf"{_ALNUM_BEFORE}(?<![.@-])[A-Za-z0-9-]++(?:\.[A-Za-z0-9-]++)++(?P<path>[/?#]{_URL_CHARS}*)?"
)
_ID_NUMBER = re.compile(
    r"(?<!\d)(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})(?!\d)"
)
# Separators inside phone numbers: spaces (also no-break, narrow and figure spaces), dots,
# parentheses and every kind of dash (also full-width forms).
_SEP = r"[ \t\u00a0\u2007\u2009\u202f().\-\u2010-\u2015\u2212\uff0d\uff08\uff09\uff0e]"
# Digit groups (any script: \d is Unicode) joined by at most two separators, never across
# lines. Letters around the number do not protect it ("tel48999991111"). A match may start
# after a separator ("ligue-48 ...", "x(48) ...") but never inside a run of digits and
# separators, which keeps scanning linear. Longer matches are masked whole as a NUMBER.
_PHONE = re.compile(
    rf"(?<!\d)(?<!\d{_SEP})(?<!\d{_SEP}{_SEP})[+\uff0b]?[(\uff08]?\d++(?:{_SEP}{{1,2}}\d++)*+(?!\d)"
)
_DATE_LIKE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2})?|\d{1,2}[.-]\d{1,2}[.-]\d{2,4}(?: \d{1,2})?"
)
_NUMBER = re.compile(r"(?<!\d)\d{6,}(?!\d)")
_MENTION = re.compile(r"(?<![\w@])@[\w.]{2,}")
_TRAILING_PUNCTUATION = ".,;:!?)_*~"  # also WhatsApp formatting marks
# Name parts never masked on their own: particles and words found in exporters' chat titles.
_NAME_PARTICLES = frozenset(
    {
        "das",
        "dos",
        "del",
        "der",
        "van",
        "von",
        "the",
        "and",
        "com",
        "with",
        "chat",
        "conversa",
        "whatsapp",
        "grupo",
        "group",
        "contato",
        "contact",
        "para",
        "for",
    }
)
_EXPORT_PREFIX = re.compile(
    r"(?i)^(?:conversa do whatsapp com|whatsapp chat with|whatsapp chat -|chat with|"
    r"conversa com)\s+"
)
_GENERIC_LABELS = frozenset({"owner", "contact", "me", "you", "eu", "voce", "você"})
_PHONE_DIGITS = (8, 15)


class Masker:
    """Masks one conversation. Placeholders are first inserted as private-use sentinels
    (`mask_tokens`), so later steps such as HTML escaping can never alter or split them, and
    are turned into `[KIND_n]` text at the end (`expand`). Private-use characters in the input
    are removed first, so content can never forge a sentinel."""

    def __init__(self, contact_names: Iterable[str], owner_names: Iterable[str]) -> None:
        self.counts: Counter[MaskKind] = Counter()
        self._values: dict[MaskKind, dict[str, int]] = {}
        self._tokens: list[str] = []
        self._kinds: list[MaskKind | None] = []
        self._contact = _name_pattern(contact_names)
        self._owner = _name_pattern(owner_names)

    def mask(self, text: str) -> str:
        return self.expand(self.mask_tokens(text))

    def mask_tokens(self, text: str) -> str:
        # Every pattern below, and the name matching, works on this one canonical form, so
        # the positions found are the positions replaced.
        text = canonical(text)
        # URLs first: an e-mail address inside a URL must not cut the URL short.
        text = _SCHEME_URL.sub(self._url, text)
        text = _EMAIL.sub(self._numbered(MaskKind.EMAIL), text)
        text = _HOST.sub(self._host, text)
        text = _ID_NUMBER.sub(self._numbered(MaskKind.ID_NUMBER), text)
        text = _PHONE.sub(self._phone, text)
        text = _NUMBER.sub(self._numbered(MaskKind.NUMBER), text)
        text = _MENTION.sub(self._numbered(MaskKind.MENTION), text)
        names = ((self._contact, MaskKind.CONTACT_NAME), (self._owner, MaskKind.OWNER_NAME))
        for pattern, kind in names:
            if pattern is not None:
                text = self._replace_names(pattern, kind, text)
        return text

    def expand(self, text: str) -> str:
        return _TOKEN.sub(lambda match: self._tokens[_decode(match.group(1))], text)

    def _token(self, placeholder: str, kind: MaskKind | None = None) -> str:
        self._tokens.append(placeholder)
        self._kinds.append(kind)
        return f"\ue000{_encode(len(self._tokens) - 1)}\ue001"

    def _replace_names(self, pattern: re.Pattern[str], kind: MaskKind, text: str) -> str:
        """Match on an accent- and case-folded copy with the same length, then replace the same
        spans in the original, so "Jose" and "JOSÉ" both match the name "José"."""
        folded = fold(text)
        parts: list[str] = []
        position = 0
        for match in pattern.finditer(folded):
            parts += [text[position : match.start()], self._token(f"[{kind.value}]", kind)]
            position = match.end()
            self.counts[kind] += 1
        return "".join(parts) + text[position:]

    def report(self) -> dict[str, int]:
        return {kind.value: self.counts[kind] for kind in MaskKind if self.counts[kind]}

    def _placeholder(self, kind: MaskKind, value: str) -> str:
        values = self._values.setdefault(kind, {})
        number = values.setdefault(value, len(values) + 1)
        self.counts[kind] += 1
        return self._token(f"[{kind.value}_{number}]", kind)

    def _numbered(self, kind: MaskKind) -> Callable[[re.Match[str]], str]:
        return lambda match: self._placeholder(kind, match.group(0).lower())

    def _url(self, match: re.Match[str]) -> str:
        value = match.group(0)
        stripped = value.rstrip(_TRAILING_PUNCTUATION)
        for inner in _TOKEN.finditer(stripped):  # placeholders swallowed by this URL
            kind = self._kinds[_decode(inner.group(1))]
            if kind is not None:
                self.counts[kind] -= 1
        return self._placeholder(MaskKind.URL, stripped.lower()) + value[len(stripped) :]

    def _host(self, match: re.Match[str]) -> str:
        value = match.group(0)
        host = value[: match.start("path") - match.start()] if match.group("path") else value
        labels = host.lower().split(".")
        has_tld = any(label in _TLDS for label in labels[1:])
        # Without a path, only lowercase host names count: "Oi.Me liga" is a sentence typed
        # without a space. Otherwise the whole host (and path) is masked, never a part of it.
        last = labels[-1]
        with_path = bool(match.group("path")) and len(last) >= 2 and any(c.isalpha() for c in last)
        if with_path or (has_tld and host == host.lower()):
            return self._url(match)
        return value

    def _phone(self, match: re.Match[str]) -> str:
        value = match.group(0)
        digits = "".join(str(unicodedata.digit(c)) for c in value if c.isdigit())
        if _DATE_LIKE.fullmatch(value.strip("() ")):
            return value
        if len(digits) > _PHONE_DIGITS[1]:
            # Several numbers in a row ("48 99999-1111 48 98888-2222") or a card number: mask
            # the whole run, so no digit is left visible.
            return self._placeholder(MaskKind.NUMBER, digits)
        if len(digits) < _PHONE_DIGITS[0]:
            return value
        return self._placeholder(MaskKind.PHONE, digits)


_PRIVATE_USE = re.compile(f"[{_SENTINELS}]")
_TOKEN = re.compile("\ue000([\ue010-\ue01f]+)\ue001")


def _encode(index: int) -> str:
    """Index in base 16 written with private-use characters, so it contains no digits."""
    return "".join(chr(0xE010 + int(d, 16)) for d in f"{index:x}")


def _decode(code: str) -> int:
    return int("".join(f"{ord(c) - 0xE010:x}" for c in code), 16)


def _name_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    variants: set[str] = set()
    for raw_name in names:
        name = " ".join(raw_name.split())
        if len(name) < 3 or name.lower() in _GENERIC_LABELS:
            continue
        variants.add(name)
        variants.update(
            part
            for part in re.findall(r"[^\W\d_]{3,}", name)  # letter runs: "Bia [Loja]" -> Loja
            if part.lower() not in _NAME_PARTICLES
        )
    if not variants:
        return None
    folded = {fold(v) for v in variants}
    alternatives = "|".join(re.escape(v) for v in sorted(folded, key=len, reverse=True))
    return re.compile(rf"{_ALNUM_BEFORE}(?:{alternatives}){_ALNUM_AFTER}")


def name_from_key(key: str) -> str:
    """The contact's name inside a conversation key such as a TXT file name
    ("WhatsApp Chat with Ana Lima" -> "Ana Lima"). Keys without a known prefix are kept."""
    return _EXPORT_PREFIX.sub("", key.strip())


# Invisible format characters (zero-width spaces and joiners, word joiner, byte order mark,
# soft hyphen, Mongolian vowel separator, bidirectional controls). They carry no visible text
# and are dropped, so they can neither split a number or a name nor shift positions.
_INVISIBLE = re.compile("[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]")


def canonical(text: str) -> str:
    """The single form every match is computed on: reserved and invisible characters removed
    first, then NFC. Removing after normalizing could leave a decomposed accent behind
    ("e" + reserved + accent), which a later normalization would compose and shift offsets."""
    return unicodedata.normalize("NFC", _INVISIBLE.sub("", _PRIVATE_USE.sub("", text)))


def fold(text: str) -> str:
    """Lowercase without accents, character by character, keeping the length unchanged (no
    normalization here: `text` is already canonical), so offsets in the folded copy are
    offsets in `text`."""
    out = []
    for char in text:
        base = unicodedata.normalize("NFD", char)[0].lower()
        out.append(base if len(base) == 1 else char)
    folded = "".join(out)
    assert len(folded) == len(text)
    return folded
