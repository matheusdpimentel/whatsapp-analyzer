"""The checked-in examples must match what the generator produces today."""

import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from whatsapp_analyzer.synthetic.generator import GeneratorSettings
from whatsapp_analyzer.synthetic.writers import write_dataset

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
REGENERATE_HINT = "Generator output changed; regenerate the examples (see examples/README.md)."


@pytest.mark.parametrize("name", ["demo-en", "demo-pt-BR"])
def test_examples_are_up_to_date(name: str, tmp_path: Path) -> None:
    example = EXAMPLES / name
    manifest = json.loads((example / "manifest.json").read_text(encoding="utf-8"))
    settings = GeneratorSettings(
        seed=manifest["seed"],
        conversations=len(manifest["conversations"]) - 2,
        locale=manifest["locale"],
    )
    summary = write_dataset(settings, tmp_path)

    assert summary.files == manifest["files"], REGENERATE_HINT
    for relative in summary.files:
        if relative.endswith(".xlsx"):
            assert _cells(example / relative) == _cells(tmp_path / relative), REGENERATE_HINT
        else:
            expected = (example / relative).read_bytes()
            assert (tmp_path / relative).read_bytes() == expected, f"{relative}: {REGENERATE_HINT}"


def _cells(path: Path) -> list[tuple[object, ...]]:
    workbook = load_workbook(path, read_only=True)
    try:
        return [tuple(row) for sheet in workbook for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
