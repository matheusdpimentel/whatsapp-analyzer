"""Identifier masking (pseudonymization). All values below are synthetic."""

import time

import pytest

from whatsapp_analyzer.llm.masking import Masker, name_from_key


def mask(text: str, contacts: tuple[str, ...] = (), owners: tuple[str, ...] = ()) -> str:
    return Masker(contacts, owners).mask(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("write to ana.lima@example.com today", "write to [EMAIL_1] today"),
        ("see https://example.org/a?b=1.", "see [URL_1]."),
        ("see www.example.com/path, ok", "see [URL_1], ok"),
        ("site shop.example.com.br/x now", "site [URL_1] now"),
        ("CPF 123.456.789-09", "CPF [ID_NUMBER_1]"),
        ("CNPJ 12.345.678/0001-95", "CNPJ [ID_NUMBER_1]"),
        ("call +55 11 91234-5678", "call [PHONE_1]"),
        ("call (11) 3456-7890 now", "call [PHONE_1] now"),
        ("call 555-0142-9999", "call [PHONE_1]"),
        ("order 98765432109876543", "order [NUMBER_1]"),
        ("code 123456", "code [NUMBER_1]"),
        ("thanks @maria.s", "thanks [MENTION_1]"),
    ],
)
def test_identifier_patterns(text: str, expected: str) -> None:
    assert mask(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "meeting on 2026-01-10 at 10:05",
        "on 10.01.2026 or 10-01-2026",
        "it costs 1.500 or 12 reais",
        "room 12345",
        "a.b is fine, ok.",
        "email me @ home",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    assert mask(text) == text


def test_numbering_is_consistent_within_a_conversation() -> None:
    masker = Masker((), ())
    first = masker.mask("a@example.com and b@example.com")
    second = masker.mask("again A@example.com")
    assert first == "[EMAIL_1] and [EMAIL_2]"
    assert second == "again [EMAIL_1]"
    assert masker.report() == {"EMAIL": 3}


def test_known_names_are_masked_in_full_and_by_part() -> None:
    text = "Hi Maria Souza, is maria there? Ask SOUZA. Thanks, Northwind Support"
    result = mask(text, contacts=("Maria Souza",), owners=("Northwind Support",))
    assert result == (
        "Hi [CONTACT_NAME], is [CONTACT_NAME] there? Ask [CONTACT_NAME]. Thanks, [OWNER_NAME]"
    )


def test_names_respect_word_boundaries_and_particles() -> None:
    result = mask("Ana, Anapolis, dos Santos", contacts=("Ana dos Santos",))
    assert result == "[CONTACT_NAME], Anapolis, dos [CONTACT_NAME]"


def test_generic_labels_and_short_names_are_not_masked() -> None:
    assert mask("the owner said ok", contacts=("Al",), owners=("owner",)) == "the owner said ok"


def test_placeholders_are_never_masked_again() -> None:
    # Placeholders are sentinels until the end, so the name "Phone" never matches "[PHONE_1]".
    masker = Masker(("Phone",), ())
    assert masker.mask("Phone +55 11 91234-5678") == "[CONTACT_NAME] [PHONE_1]"


@pytest.mark.parametrize(
    "text",
    [
        "meus numeros: 48 99999-1111 48 98888-2222",
        "cartao 4111 1111 1111 1111",
        "fone 48999991111 e 48988882222",
    ],
)
def test_adjacent_numbers_never_leave_digits_visible(text: str) -> None:
    masked = mask(text)
    assert not any(ch.isdigit() for ch in masked.replace("_1", "").replace("_2", ""))


def test_numbers_on_different_lines_are_masked_separately() -> None:
    assert mask("call +55 11 91234-5678\n2026 was good") == "call [PHONE_1]\n2026 was good"


@pytest.mark.parametrize("text", ["on 2026-01-01 00:00 ok", "em 10.01.2026 10h"])
def test_date_times_are_not_phones(text: str) -> None:
    assert mask(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("linktr.ee/fulano", "[URL_1]"),
        ("pix.example.xyz/abc?token=S", "[URL_1]"),
        ("visit loja.shop today", "visit [URL_1] today"),
        ("meusite.online/x.", "[URL_1]."),
    ],
)
def test_urls_without_scheme(text: str, expected: str) -> None:
    assert mask(text) == expected


def test_names_match_without_accents_and_in_any_normalization() -> None:
    nfd = "Jose\u0301"
    result = mask(f"Jose, ANTONIO, {nfd}, josé", contacts=("José Antônio",))
    assert result == "[CONTACT_NAME], [CONTACT_NAME], [CONTACT_NAME], [CONTACT_NAME]"


@pytest.mark.parametrize(
    ("key", "name"),
    [
        ("Conversa do WhatsApp com Joana Souza", "Joana Souza"),
        ("WhatsApp Chat with Ana Lima", "Ana Lima"),
        ("chat_0001", "chat_0001"),
    ],
)
def test_contact_name_from_conversation_key(key: str, name: str) -> None:
    assert name_from_key(key) == name


def test_words_from_chat_titles_are_not_masked_as_names() -> None:
    masker = Masker([name_from_key("Conversa do WhatsApp com Joana Souza"), "Chat com Equipe"], [])
    text = "Oi Joana, vamos com tudo! Chat aberto com a equipe"
    assert (
        masker.mask(text) == "Oi [CONTACT_NAME], vamos com tudo! Chat aberto com a [CONTACT_NAME]"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("meu numero &#91;48999991111]", "meu numero &#91;[PHONE_1]]"),
        ("tel 48 99999-1111]", "tel [PHONE_1]]"),
        ("id 1234567]", "id [NUMBER_1]]"),
    ],
)
def test_numbers_before_brackets_are_masked(text: str, expected: str) -> None:
    assert mask(text) == expected


def test_urls_stop_at_angle_brackets() -> None:
    assert mask("<https://x.com/a> depois") == "<[URL_1]> depois"


@pytest.mark.parametrize("text", ["a." * 8000, "x" * 30000, "1-" * 8000 + "]", "a@" * 8000])
def test_masking_time_is_linear_on_pathological_input(text: str) -> None:
    started = time.perf_counter()
    mask(text)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Entra no meusite.com.br.", "Entra no [URL_1]."),
        ("veja loja.com.", "veja [URL_1]."),
        ("acesse prefeitura.sc.gov.br... ok", "acesse [URL_1]... ok"),
        ("site.com.br. Depois", "[URL_1]. Depois"),
    ],
)
def test_domains_at_the_end_of_a_sentence(text: str, expected: str) -> None:
    assert mask(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("_48 99999-1111_", "_[PHONE_1]_"),
        ("_www.site.com.br_", "_[URL_1]_"),
        ("_https://x.com/a_", "_[URL_1]_"),
        ("*Joana* ~48 99999-1111~", "*[CONTACT_NAME]* ~[PHONE_1]~"),
        ("_Joana_", "_[CONTACT_NAME]_"),
    ],
)
def test_whatsapp_formatting_marks_do_not_hide_identifiers(text: str, expected: str) -> None:
    assert mask(text, contacts=("Joana",)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("ligue-48 99999-1111", "ligue-[PHONE_1]"), ("x(48) 99999-1111", "x[PHONE_1]")],
)
def test_area_codes_after_punctuation_are_included(text: str, expected: str) -> None:
    assert mask(text) == expected


@pytest.mark.parametrize("text", ["1 " * 8000 + "x", "1-" * 8000 + "x", "_a." * 8000])
def test_more_pathological_inputs_stay_fast(text: str) -> None:
    started = time.perf_counter()
    mask(text)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("text", ["1--" * 16000 + "1a", "1 -" * 16000 + "1a", "1-(" * 16000 + "1a"])
def test_phone_runs_with_double_separators_stay_fast(text: str) -> None:
    started = time.perf_counter()
    mask(text)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "text", ["Oi.Me liga amanhã", "Sim.Com certeza", "beleza.To indo", "fim.Top"]
)
def test_sentences_without_a_space_are_not_domains(text: str) -> None:
    assert mask(text) == text
