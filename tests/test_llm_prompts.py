"""Prompt templates: format, validation, rendering, hashing and the built-in manifest."""

import tomllib
from importlib import resources
from pathlib import Path

import pytest

from whatsapp_analyzer.hashing import sha256_hex
from whatsapp_analyzer.llm.prompts import (
    PromptError,
    builtin_templates,
    find_template,
    normalize,
    parse_template,
    render,
)

TEMPLATE = """+++
id = "demo"
version = "1.2.0"
description = "Demo"
output_schema = "engagement-output/v1"
variables = ["conversation", "count"]
max_output_tokens = 100
+++

## system
Classify. {{count}} messages.

## user
<conversation>
{{ conversation }}
</conversation>
"""


def test_parse_and_render() -> None:
    template = parse_template(TEMPLATE, "test")
    assert (template.id, template.version, template.variables) == (
        "demo",
        "1.2.0",
        ("conversation", "count"),
    )
    rendered = render(template, {"conversation": "[m1] hi", "count": "1"}, "model-a")
    assert rendered.system == "Classify. 1 messages.\n"
    assert "[m1] hi" in rendered.user
    assert "{{" not in rendered.user


def test_values_are_inserted_once_and_never_expanded() -> None:
    template = parse_template(TEMPLATE, "test")
    rendered = render(template, {"conversation": "{{count}} {{ evil }}", "count": "7"}, "m")
    assert "{{count}} {{ evil }}" in rendered.user


def test_render_requires_exactly_the_declared_values() -> None:
    template = parse_template(TEMPLATE, "test")
    with pytest.raises(PromptError) as missing:
        render(template, {"conversation": "x"}, "m")
    assert missing.value.kind == "values_mismatch"
    with pytest.raises(PromptError):
        render(template, {"conversation": "x", "count": "1", "extra": "y"}, "m")


def test_hashes() -> None:
    template = parse_template(TEMPLATE, "test")
    assert template.template_hash == sha256_hex(normalize(TEMPLATE))
    assert (
        parse_template(TEMPLATE.replace("\n", "\r\n"), "x").template_hash == template.template_hash
    )
    values = {"conversation": "x", "count": "1"}
    assert render(template, values, "a").prompt_hash == render(template, values, "a").prompt_hash
    assert render(template, values, "a").prompt_hash != render(template, values, "b").prompt_hash
    assert (
        render(template, values, "a").prompt_hash
        != render(template, {**values, "count": "2"}, "a").prompt_hash
    )


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("## system\nx\n## user\ny\n", "missing_front_matter"),
        (TEMPLATE.replace('id = "demo"', 'id = "demo'), "front_matter_syntax"),
        (TEMPLATE.replace('id = "demo"', 'id = "Demo Template"'), "invalid_id"),
        (TEMPLATE.replace('version = "1.2.0"', 'version = "1.2"'), "invalid_version"),
        (TEMPLATE.replace('description = "Demo"\n', ""), "missing_key"),
        (
            TEMPLATE.replace('description = "Demo"', 'description = "Demo"\nextra = 1'),
            "unknown_key",
        ),
        (TEMPLATE.replace("engagement-output/v1", "other/v9"), "unknown_output_schema"),
        (TEMPLATE.replace('"conversation", "count"', '"conversation"'), "variables_mismatch"),
        (
            TEMPLATE.replace('"conversation", "count"', '"conversation", "count", "x"'),
            "variables_mismatch",
        ),
        (TEMPLATE.replace("## user", "## assistant"), "unknown_section"),
        (TEMPLATE.replace("## user\n", "## system\n"), "duplicate_section"),
        (TEMPLATE.replace("+++\n\n## system", "+++\nhello\n## system"), "text_before_section"),
        (TEMPLATE.replace("max_output_tokens = 100", "max_output_tokens = 0"), "wrong_type"),
    ],
)
def test_invalid_templates_are_rejected(text: str, kind: str) -> None:
    with pytest.raises(PromptError) as info:
        parse_template(text, "test")
    assert info.value.kind == kind


def test_builtin_templates_match_the_manifest() -> None:
    templates = builtin_templates()
    root = resources.files("whatsapp_analyzer.llm").joinpath("prompts")
    manifest = tomllib.loads(root.joinpath("manifest.toml").read_text(encoding="utf-8"))
    assert [(t.id, t.version, t.template_hash) for t in templates] == [
        (e["id"], e["version"], e["sha256"]) for e in manifest["templates"]
    ]
    for entry in manifest["templates"]:
        text = root.joinpath(entry["file"]).read_text(encoding="utf-8")
        assert sha256_hex(normalize(text)) == entry["sha256"], entry["file"]


def test_builtin_template_mentions_only_observable_behavior() -> None:
    template = find_template(None)
    text = (template.system + template.user).lower()
    assert "untrusted data" in text
    for forbidden in ("political", "vote", "religion", "ethnicity", "sentiment score"):
        assert forbidden not in text


def test_find_template_by_id_version_file_and_workspace(tmp_path: Path) -> None:
    assert find_template("engagement-classification@1.0.0").version == "1.0.0"
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "demo.md").write_text(TEMPLATE, encoding="utf-8")
    assert find_template("demo", prompts).origin == "demo.md"
    assert find_template(str(prompts / "demo.md")).id == "demo"
    with pytest.raises(PromptError) as unknown:
        find_template("missing", prompts)
    assert unknown.value.kind == "unknown_template"
    (prompts / "copy.md").write_text(TEMPLATE, encoding="utf-8")
    with pytest.raises(PromptError) as duplicate:
        find_template("demo", prompts)
    assert duplicate.value.kind == "duplicate_template"
