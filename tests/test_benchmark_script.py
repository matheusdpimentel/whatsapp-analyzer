"""scripts/benchmark.py must report measured counts, not physical lines or requested sizes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("wca_benchmark", ROOT / "scripts/benchmark.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_count_dataset_uses_csv_records_and_distinct_conversations(tmp_path: Path) -> None:
    path = tmp_path / "c.csv"
    path.write_text(
        "conversation_id,timestamp,sender,direction,text\n"
        'a,2026-01-01T10:00:00,X,inbound,"one\ntwo\nthree"\n'  # 1 record, 3 physical lines
        "a,2026-01-01T10:01:00,Y,outbound,hello\n"
        "b,2026-01-01T10:02:00,X,inbound,hi\n",
        encoding="utf-8",
    )
    assert _load().count_dataset(path) == (2, 3)  # not 6 lines - 1, not a requested size


def test_count_dataset_agrees_with_the_generator_manifest() -> None:
    """The manifest is written by the generator, independently of the CSV reader."""
    folder = ROOT / "examples" / "demo-en"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    expected = (
        len(manifest["conversations"]),
        sum(c["messages"] for c in manifest["conversations"]),
    )
    assert _load().count_dataset(folder / "conversations.csv") == expected
