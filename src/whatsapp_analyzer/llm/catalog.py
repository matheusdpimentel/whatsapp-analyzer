"""Open model catalog: known capabilities and limits per (provider, model).

The built-in `models.toml` lists models this project has verified, with the source and the
date of each entry. A workspace `models.toml` (same format) adds models or replaces built-in
entries. A model that is in neither file can still be used: its capabilities are simply
unknown, and every check that needs one says how to declare it instead of guessing.
"""

import tomllib
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any

from whatsapp_analyzer.config import ConfigError

CATALOG_SCHEMA = 1
MODELS_FILE = "models.toml"
_INTEGERS = ("context_window", "max_input_tokens", "max_output_tokens")
_BOOLEANS = ("temperature", "cache_write_billed", "prompt_caching")
_FIELDS = {"provider", "model", "structured_output", "source", "retrieved_at"}
_FIELDS |= set(_INTEGERS) | set(_BOOLEANS)


class OutputMode(StrEnum):
    JSON_SCHEMA = "json_schema"  # the provider enforces the JSON Schema
    JSON_OBJECT = "json_object"  # the provider guarantees JSON, the prompt describes the shape
    NONE = "none"  # only the prompt asks for JSON; local validation decides


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    temperature: bool | None = None  # None: unknown
    structured_output: OutputMode | None = None
    cache_write_billed: bool | None = None
    prompt_caching: bool | None = None  # False: no prompt cache, cache counts are zero
    source: str | None = None
    retrieved_at: date | None = None
    origin: str | None = None  # "built-in", the workspace file name, or None (not listed)

    @property
    def listed(self) -> bool:
        return self.origin is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "listed": self.listed,
            "origin": self.origin,
            "context_window": self.context_window,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "structured_output": self.structured_output.value if self.structured_output else None,
            "cache_write_billed": self.cache_write_billed,
            "prompt_caching": self.prompt_caching,
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
        }


class Catalog:
    def __init__(self, entries: list[ModelInfo]) -> None:
        self.entries = {(e.provider, e.model): e for e in entries}

    def lookup(self, provider: str, model: str) -> ModelInfo:
        return self.entries.get((provider, model)) or ModelInfo(provider, model)

    def merged(self, other: "Catalog") -> "Catalog":
        """`other` wins: a workspace entry replaces the built-in entry for the same model."""
        return Catalog([*self.entries.values(), *other.entries.values()])


def builtin_catalog() -> Catalog:
    text = resources.files(__package__).joinpath(MODELS_FILE).read_text(encoding="utf-8")
    return parse_catalog(tomllib.loads(text), "built-in")


def load_catalog(workspace_file: Path | None) -> Catalog:
    catalog = builtin_catalog()
    if workspace_file is None or not workspace_file.is_file():
        return catalog
    try:
        data = tomllib.loads(workspace_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError("unreadable", path=str(workspace_file)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("syntax", path=str(workspace_file), reason=str(exc)) from exc
    return catalog.merged(parse_catalog(data, workspace_file.name))


def parse_catalog(data: dict[str, Any], origin: str) -> Catalog:
    unknown = sorted(set(data) - {"schema", "models"})
    if unknown:
        raise ConfigError("unknown_key", key=unknown[0])
    if type(data.get("schema")) is not int or data["schema"] != CATALOG_SCHEMA:
        raise ConfigError(
            "out_of_range", key="schema", minimum=CATALOG_SCHEMA, maximum=CATALOG_SCHEMA
        )
    entries = data.get("models", [])
    if not isinstance(entries, list):
        raise ConfigError("wrong_type", key="models", expected="array of tables")
    models = [_entry(entry, index, origin) for index, entry in enumerate(entries)]
    keys = [(m.provider, m.model) for m in models]
    if len(keys) != len(set(keys)):
        raise ConfigError("duplicate_model", key="models")
    return Catalog(models)


def _entry(entry: object, index: int, origin: str) -> ModelInfo:
    path = f"models[{index}]."
    if not isinstance(entry, dict):
        raise ConfigError("wrong_type", key=path.rstrip("."), expected="table")
    unknown = sorted(set(entry) - _FIELDS)
    if unknown:
        raise ConfigError("unknown_key", key=f"{path}{unknown[0]}")
    for name in ("provider", "model", "source"):
        if name not in entry:
            raise ConfigError("missing_key", key=f"{path}{name}")
        if not isinstance(entry[name], str) or not entry[name].strip():
            raise ConfigError("wrong_type", key=f"{path}{name}", expected="string")
    integers, booleans = _integers(entry, path), _booleans(entry, path)
    mode = None
    if "structured_output" in entry:
        try:
            mode = OutputMode(entry["structured_output"])
        except ValueError as exc:
            choices = ", ".join(m.value for m in OutputMode)
            raise ConfigError(
                "invalid_choice", key=f"{path}structured_output", choices=choices
            ) from exc
    retrieved = entry.get("retrieved_at")
    if retrieved is not None and type(retrieved) is not date:
        raise ConfigError("wrong_type", key=f"{path}retrieved_at", expected="date")
    return ModelInfo(
        provider=entry["provider"],
        model=entry["model"],
        context_window=integers["context_window"],
        max_input_tokens=integers["max_input_tokens"],
        max_output_tokens=integers["max_output_tokens"],
        temperature=booleans["temperature"],
        structured_output=mode,
        cache_write_billed=booleans["cache_write_billed"],
        prompt_caching=booleans["prompt_caching"],
        source=entry["source"],
        retrieved_at=retrieved,
        origin=origin,
    )


def _integers(entry: dict[str, Any], path: str) -> dict[str, int | None]:
    values: dict[str, int | None] = {}
    for name in _INTEGERS:
        value = entry.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ConfigError("wrong_type", key=f"{path}{name}", expected="integer")
        if value is not None and not 1 <= value <= 100_000_000:
            raise ConfigError("out_of_range", key=f"{path}{name}", minimum=1, maximum=100_000_000)
        values[name] = value
    return values


def _booleans(entry: dict[str, Any], path: str) -> dict[str, bool | None]:
    values: dict[str, bool | None] = {}
    for name in _BOOLEANS:
        value = entry.get(name)
        if value is not None and not isinstance(value, bool):
            raise ConfigError("wrong_type", key=f"{path}{name}", expected="boolean")
        values[name] = value
    return values
