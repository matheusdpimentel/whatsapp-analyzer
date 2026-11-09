"""Local validation of structured responses."""

import pytest

from whatsapp_analyzer.llm.output import validate_output
from whatsapp_analyzer.llm.types import Classification, Label, ReasonCode, ValidationFailure

IDS = frozenset({"m1", "m2", "m3"})


def test_valid_response() -> None:
    result = validate_output(
        '{"level": "HIGH", "reasons": ["ASKS_QUESTIONS"], "evidence": ["m2"]}', IDS
    )
    assert result == Classification(Label.HIGH, (ReasonCode.ASKS_QUESTIONS,), ("m2",))


def test_review_required_is_a_valid_conclusion() -> None:
    result = validate_output(
        '{"level": "REVIEW_REQUIRED", "reasons": ["INSUFFICIENT_CONTEXT"], "evidence": []}', IDS
    )
    assert isinstance(result, Classification) and result.label is Label.REVIEW_REQUIRED


def test_one_fenced_block_is_accepted() -> None:
    text = '```json\n{"level": "LOW", "reasons": ["NO_REPLY"], "evidence": []}\n```'
    assert isinstance(validate_output(text, IDS), Classification)


@pytest.mark.parametrize(
    ("text", "code", "field"),
    [
        ("not json", "NOT_JSON", None),
        ('Sure! {"level": "LOW", "reasons": ["NO_REPLY"], "evidence": []}', "NOT_JSON", None),
        (
            '{"level": "LOW", "level": "HIGH", "reasons": ["NO_REPLY"], "evidence": []}',
            "DUPLICATE_KEY",
            None,
        ),
        ('{"level": NaN, "reasons": [], "evidence": []}', "NOT_JSON", None),
        ('["LOW"]', "NOT_AN_OBJECT", None),
        ('{"level": "LOW", "reasons": ["NO_REPLY"]}', "MISSING_FIELD", "evidence"),
        (
            '{"level": "LOW", "reasons": ["NO_REPLY"], "evidence": [], "why": "x"}',
            "UNKNOWN_FIELD",
            "why",
        ),
        ('{"level": "low", "reasons": ["NO_REPLY"], "evidence": []}', "INVALID_VALUE", "level"),
        ('{"level": "LOW", "reasons": [], "evidence": []}', "INVALID_VALUE", "reasons"),
        (
            '{"level": "LOW", "reasons": ["NO_REPLY", "NO_REPLY"], "evidence": []}',
            "INVALID_VALUE",
            "reasons",
        ),
        ('{"level": "LOW", "reasons": ["IS_VOTER"], "evidence": []}', "INVALID_VALUE", "reasons"),
        (
            '{"level": "LOW", "reasons": ["NO_REPLY"], "evidence": ["message 2"]}',
            "INVALID_VALUE",
            "evidence",
        ),
        (
            '{"level": "LOW", "reasons": ["NO_REPLY"], "evidence": ["m7"]}',
            "UNKNOWN_EVIDENCE",
            "evidence",
        ),
        ('{"level": "LOW", "reasons": ["NO_REPLY"], "evidence": [3]}', "INVALID_VALUE", "evidence"),
        ("x" * 5000, "TOO_LONG", None),
    ],
)
def test_invalid_responses(text: str, code: str, field: str | None) -> None:
    assert validate_output(text, IDS) == ValidationFailure(code, field)


def test_deep_nesting_is_not_json_instead_of_a_crash() -> None:
    assert validate_output("[" * 3000, IDS) == ValidationFailure("NOT_JSON")


def test_evidence_ids_must_match_exactly() -> None:
    text = '{"level": "LOW", "reasons": ["NO_REPLY"], "evidence": ["m1\\n"]}'
    assert validate_output(text, IDS) == ValidationFailure("INVALID_VALUE", "evidence")


@pytest.mark.parametrize(
    ("level", "evidence", "ok"),
    [
        ("HIGH", [], False),
        ("MEDIUM", ["m1"], False),
        ("HIGH", ["m2"], True),
        ("LOW", [], True),
        ("REVIEW_REQUIRED", [], True),
    ],
)
def test_high_and_medium_must_cite_a_contact_message(
    level: str, evidence: list[str], ok: bool
) -> None:
    ids = ", ".join(f'"{e}"' for e in evidence)
    text = f'{{"level": "{level}", "reasons": ["CONFIRMS_OR_AGREES"], "evidence": [{ids}]}}'
    result = validate_output(text, IDS, contact_ids=frozenset({"m2"}))
    assert isinstance(result, Classification) is ok
    if not ok:
        assert result == ValidationFailure("EVIDENCE_REQUIRED", "evidence")
