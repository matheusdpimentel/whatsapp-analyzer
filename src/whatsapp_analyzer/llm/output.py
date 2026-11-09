"""Local validation of the structured model response (`engagement-output/v1`).

The response must be exactly one JSON object with `level`, `reasons` and `evidence`: no other
fields, closed enums, and evidence ids that exist in the context that was sent. One fenced code
block around the JSON is accepted, because models often add it; anything else is rejected.
A failure is an execution status (INVALID_RESPONSE), never a classification label.
"""

import copy
import json
import re
from typing import Any

from whatsapp_analyzer.llm.types import Classification, Label, ReasonCode, ValidationFailure

OUTPUT_SCHEMA_ID = "engagement-output/v1"
MAX_RESPONSE_CHARS = 4000
MAX_REASONS = 5
MAX_EVIDENCE = 5
_FENCED = re.compile(r"^```(?:json)?[ \t]*\n(.*)\n```$", re.DOTALL)
_MESSAGE_ID = re.compile(r"^m[1-9]\d*$")
_FIELDS = ("level", "reasons", "evidence")

_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_FIELDS),
    "properties": {
        "level": {"type": "string", "enum": [label.value for label in Label]},
        "reasons": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_REASONS,
            "uniqueItems": True,
            "items": {"type": "string", "enum": [code.value for code in ReasonCode]},
        },
        "evidence": {
            "type": "array",
            "maxItems": MAX_EVIDENCE,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": _MESSAGE_ID.pattern},
        },
    },
}


def output_schema() -> dict[str, object]:
    """A fresh copy of the JSON Schema for each request, so no caller (or adapter) can change
    the schema seen by later requests."""
    return copy.deepcopy(_OUTPUT_SCHEMA)


class _DuplicateKeyError(ValueError):
    pass


def validate_output(
    text: str, allowed_ids: frozenset[str], contact_ids: frozenset[str] | None = None
) -> Classification | ValidationFailure:
    """`contact_ids`: ids of the contact's messages in the context. HIGH and MEDIUM describe the
    contact's participation, so they must cite at least one of them (when given)."""
    data = _parse(text)
    if isinstance(data, ValidationFailure):
        return data
    failure = _check_fields(data) or _check_values(data, allowed_ids)
    if (
        failure is None
        and contact_ids is not None
        and data["level"] in {"HIGH", "MEDIUM"}
        and not set(data["evidence"]) & contact_ids
    ):
        failure = ValidationFailure("EVIDENCE_REQUIRED", "evidence")
    if failure is not None:
        return failure
    return Classification(
        label=Label(data["level"]),
        reasons=tuple(ReasonCode(r) for r in data["reasons"]),
        evidence=tuple(data["evidence"]),
    )


def _parse(text: str) -> dict[str, Any] | ValidationFailure:
    if len(text) > MAX_RESPONSE_CHARS:
        return ValidationFailure("TOO_LONG")
    body = text.strip()
    if fenced := _FENCED.match(body):
        body = fenced.group(1).strip()
    try:
        data = json.loads(body, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except _DuplicateKeyError:
        return ValidationFailure("DUPLICATE_KEY")
    except (ValueError, RecursionError):  # deeply nested input must not crash the caller
        return ValidationFailure("NOT_JSON")
    if not isinstance(data, dict):
        return ValidationFailure("NOT_AN_OBJECT")
    return data


def _check_fields(data: dict[str, Any]) -> ValidationFailure | None:
    for name in _FIELDS:
        if name not in data:
            return ValidationFailure("MISSING_FIELD", name)
    extra = sorted(set(data) - set(_FIELDS))
    return ValidationFailure("UNKNOWN_FIELD", extra[0]) if extra else None


def _check_values(data: dict[str, Any], allowed_ids: frozenset[str]) -> ValidationFailure | None:
    level, reasons, evidence = data["level"], data["reasons"], data["evidence"]
    if not isinstance(level, str) or level not in Label.__members__:
        return ValidationFailure("INVALID_VALUE", "level")
    if not _unique_strings(reasons, 1, MAX_REASONS) or any(
        r not in ReasonCode.__members__ for r in reasons
    ):
        return ValidationFailure("INVALID_VALUE", "reasons")
    if not _unique_strings(evidence, 0, MAX_EVIDENCE) or not all(
        _MESSAGE_ID.fullmatch(e) for e in evidence
    ):
        return ValidationFailure("INVALID_VALUE", "evidence")
    if any(e not in allowed_ids for e in evidence):
        return ValidationFailure("UNKNOWN_EVIDENCE", "evidence")
    return None


def _unique_strings(value: object, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and minimum <= len(value) <= maximum
        and all(isinstance(item, str) for item in value)
        and len(set(value)) == len(value)
    )


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKeyError
    return dict(pairs)


def _no_constants(name: str) -> object:
    raise ValueError(name)  # NaN and Infinity are not JSON
