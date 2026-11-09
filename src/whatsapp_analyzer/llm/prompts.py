"""Prompt templates: Markdown with a TOML front matter, versioned and hashed.

    +++
    id = "engagement-classification"
    version = "1.0.0"
    description = "..."
    output_schema = "engagement-output/v1"
    variables = ["conversation", "message_count"]
    max_output_tokens = 300
    +++

    ## system
    ...instructions...

    ## user
    ...{{conversation}}...

There is no logic: only `## system` and `## user` sections and `{{name}}` placeholders. The
declared variables must be exactly the placeholders used, and rendering needs exactly those
values; nothing is filled in or dropped silently. Values are inserted in one pass, so a value
that contains `{{...}}` is never expanded again.

Built-in templates are listed in `prompts/manifest.toml` with their SHA-256. A test fails if a
file changes without the manifest being updated; changing a released template requires a new
version (a new file), never an edit in place.
"""

import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from whatsapp_analyzer.hashing import canonical_json, sha256_hex

FRONT_MATTER_DELIMITER = "+++"
SECTIONS = ("system", "user")
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
KNOWN_OUTPUT_SCHEMAS = frozenset({"engagement-output/v1"})
_FRONT_MATTER_KEYS = {
    "id",
    "version",
    "description",
    "output_schema",
    "variables",
    "max_output_tokens",
}


class PromptError(Exception):
    def __init__(self, kind: str, **details: object) -> None:
        super().__init__(kind)
        self.kind = kind
        self.details = details


@dataclass(frozen=True)
class PromptTemplate:
    id: str
    version: str
    description: str
    output_schema: str
    variables: tuple[str, ...]
    max_output_tokens: int
    system: str
    user: str
    template_hash: str
    origin: str  # "built-in" or a file name

    def summary(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "output_schema": self.output_schema,
            "variables": list(self.variables),
            "max_output_tokens": self.max_output_tokens,
            "template_hash": self.template_hash,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class RenderedPrompt:
    system: str
    user: str
    prompt_hash: str

    def __repr__(self) -> str:  # never show content
        return f"RenderedPrompt(prompt_hash={self.prompt_hash[:12]!r})"


def normalize(text: str) -> str:
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def parse_template(text: str, origin: str) -> PromptTemplate:
    text = normalize(text)
    lines = text.split("\n")
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise PromptError("missing_front_matter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == FRONT_MATTER_DELIMITER)
    except StopIteration as exc:
        raise PromptError("missing_front_matter") from exc
    try:
        meta = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise PromptError("front_matter_syntax", reason=str(exc)) from exc
    sections = _sections(lines[end + 1 :])
    fields = _front_matter(meta)

    used = set(PLACEHOLDER.findall(sections["system"])) | set(PLACEHOLDER.findall(sections["user"]))
    declared = set(fields.variables)
    if used != declared:
        raise PromptError(
            "variables_mismatch",
            undeclared=", ".join(sorted(used - declared)) or "-",
            unused=", ".join(sorted(declared - used)) or "-",
        )
    return PromptTemplate(
        id=fields.id,
        version=fields.version,
        description=fields.description,
        output_schema=fields.output_schema,
        variables=fields.variables,
        max_output_tokens=fields.max_output_tokens,
        system=sections["system"],
        user=sections["user"],
        template_hash=sha256_hex(text),
        origin=origin,
    )


@dataclass(frozen=True)
class _Meta:
    id: str
    version: str
    description: str
    output_schema: str
    variables: tuple[str, ...]
    max_output_tokens: int


def _front_matter(meta: dict[str, object]) -> _Meta:
    unknown = sorted(set(meta) - _FRONT_MATTER_KEYS)
    if unknown:
        raise PromptError("unknown_key", key=unknown[0])
    for key in sorted(_FRONT_MATTER_KEYS):
        if key not in meta:
            raise PromptError("missing_key", key=key)
    for key in ("id", "version", "description", "output_schema"):
        if not isinstance(meta[key], str):
            raise PromptError("wrong_type", key=key, expected="string")
    if not TEMPLATE_ID.match(str(meta["id"])):
        raise PromptError("invalid_id", key="id")
    if not VERSION.match(str(meta["version"])):
        raise PromptError("invalid_version", key="version")
    if meta["output_schema"] not in KNOWN_OUTPUT_SCHEMAS:
        raise PromptError("unknown_output_schema", key="output_schema")
    variables = meta["variables"]
    if (
        not isinstance(variables, list)
        or not all(isinstance(v, str) and PLACEHOLDER.fullmatch("{{" + v + "}}") for v in variables)
        or len(set(variables)) != len(variables)
    ):
        raise PromptError("wrong_type", key="variables", expected="list of unique names")
    tokens = meta["max_output_tokens"]
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= 32_000:
        raise PromptError("wrong_type", key="max_output_tokens", expected="integer 1..32000")
    return _Meta(
        id=str(meta["id"]),
        version=str(meta["version"]),
        description=str(meta["description"]),
        output_schema=str(meta["output_schema"]),
        variables=tuple(str(v) for v in variables),
        max_output_tokens=tokens,
    )


def _sections(lines: list[str]) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if line.startswith("## "):
            name = line[3:].strip()
            if name not in SECTIONS:
                raise PromptError("unknown_section", section=name)
            if name in sections:
                raise PromptError("duplicate_section", section=name)
            sections[name] = []
            current = name
        elif current is None:
            if line.strip():
                raise PromptError("text_before_section")
        else:
            sections[current].append(line)
    for name in SECTIONS:
        if name not in sections or not "\n".join(sections[name]).strip():
            raise PromptError("missing_section", section=name)
    return {name: "\n".join(body).strip() + "\n" for name, body in sections.items()}


def render(template: PromptTemplate, values: dict[str, str], model: str) -> RenderedPrompt:
    expected, given = set(template.variables), set(values)
    if expected != given:
        raise PromptError(
            "values_mismatch",
            missing=", ".join(sorted(expected - given)) or "-",
            unexpected=", ".join(sorted(given - expected)) or "-",
        )

    def fill(text: str) -> str:
        return PLACEHOLDER.sub(lambda match: values[match.group(1)], text)

    system, user = fill(template.system), fill(template.user)
    prompt_hash = sha256_hex(
        canonical_json(
            {
                "template": [template.id, template.version, template.template_hash],
                "model": model,
                "max_output_tokens": template.max_output_tokens,
                "system": system,
                "user": user,
            }
        )
    )
    return RenderedPrompt(system, user, prompt_hash)


def load_template_file(path: Path) -> PromptTemplate:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError("file_not_found", path=str(path)) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError("unreadable", path=str(path)) from exc
    return parse_template(text, path.name)


def builtin_templates() -> list[PromptTemplate]:
    """Built-in templates, verified against the manifest."""
    root = resources.files(__package__).joinpath("prompts")
    manifest = tomllib.loads(root.joinpath("manifest.toml").read_text(encoding="utf-8"))
    templates = []
    for entry in manifest["templates"]:
        text = root.joinpath(entry["file"]).read_text(encoding="utf-8")
        template = parse_template(text, "built-in")
        if (template.id, template.version, template.template_hash) != (
            entry["id"],
            entry["version"],
            entry["sha256"],
        ):
            raise PromptError("manifest_mismatch", file=entry["file"])
        templates.append(template)
    return templates


@dataclass(frozen=True)
class InvalidTemplate:
    file: str
    error: str  # PromptError kind


def workspace_templates(
    directory: Path | None,
) -> tuple[list[PromptTemplate], list[InvalidTemplate]]:
    """Valid templates in `<workspace>/prompts/`, and the invalid files with their error. An
    invalid file never prevents the valid ones from being used."""
    if directory is None or not directory.is_dir():
        return [], []
    valid: list[PromptTemplate] = []
    invalid: list[InvalidTemplate] = []
    for path in sorted(directory.glob("*.md")):
        try:
            valid.append(load_template_file(path))
        except PromptError as exc:
            invalid.append(InvalidTemplate(path.name, exc.kind))
    return valid, invalid


def find_template(
    reference: str | None,
    workspace_prompts: Path | None = None,
    base_dir: Path | None = None,
) -> PromptTemplate:
    """`reference`: a template file path, `id` or `id@version`. Default: the latest built-in
    engagement template. Workspace templates (`<workspace>/prompts/*.md`) are also searched.

    A relative file path is resolved against `base_dir` when given (the workspace, for paths
    written in analyzer.toml), otherwise against the current directory (command-line paths).
    """
    if reference is not None and reference.endswith(".md"):
        path = Path(reference)
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        return load_template_file(path)
    found, invalid = workspace_templates(workspace_prompts)
    available = builtin_templates() + found
    wanted_id, _, wanted_version = (reference or DEFAULT_TEMPLATE).partition("@")
    matches = [
        t
        for t in available
        if t.id == wanted_id and (not wanted_version or t.version == wanted_version)
    ]
    versions = [(t.id, t.version) for t in matches]
    duplicates = sorted({f"{i}@{v}" for i, v in versions if versions.count((i, v)) > 1})
    if duplicates:
        raise PromptError("duplicate_template", reference=duplicates[0])
    if not matches:
        if invalid:
            raise PromptError(
                "unknown_template_with_invalid",
                reference=reference or DEFAULT_TEMPLATE,
                invalid=", ".join(item.file for item in invalid),
            )
        raise PromptError("unknown_template", reference=reference or DEFAULT_TEMPLATE)
    return max(matches, key=lambda t: tuple(int(part) for part in t.version.split(".")))


DEFAULT_TEMPLATE = "engagement-classification"
