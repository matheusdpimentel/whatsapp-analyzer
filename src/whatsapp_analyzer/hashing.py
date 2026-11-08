"""Stable hashes for configurations and conversation contents.

Every hash is SHA-256 over canonical JSON (sorted keys, fixed separators), so the same value
produces the same digest on every platform and Python version.
"""

import hashlib
import json
from datetime import datetime

SHORT_HASH_LENGTH = 12


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_hash(kind: str, version: int, config: dict[str, object]) -> str:
    """Hash of a configuration together with the version of the code that interprets it."""
    return sha256_hex(canonical_json({"kind": kind, "version": version, "config": config}))


def short(digest: str) -> str:
    return digest[:SHORT_HASH_LENGTH]


def format_timestamp(value: datetime) -> str:
    """Fixed-width ISO 8601 (always with microseconds), so text order equals time order."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
