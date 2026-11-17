"""Run the documented workflow end to end against an installed `whatsapp-analyzer`.

Usage: python scripts/check_quickstart.py [PATH-TO-whatsapp-analyzer]

Everything happens in a temporary directory with synthetic data and the offline fake
provider: no network, no real conversation, no cost. Exit code 0 means every documented
step worked; the first failing step is printed with its output and the script stops.

The same script verifies an installed wheel (see docs/releasing.md) and, from the test
suite, the development install.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

MAPPING = """\
[columns]
conversation_id = "chat_id"
timestamp = "sent_at"
sender = "author"
direction = "from_me"
text = "body"
message_type = "kind"

[timestamp]
format = "%d/%m/%Y %H:%M:%S"

[values.direction]
"true" = "outbound"
"false" = "inbound"
"""


def find_executable(argument: str | None) -> str:
    if argument:
        return argument
    found = shutil.which("whatsapp-analyzer")
    if found is None:
        sys.exit("whatsapp-analyzer not found on PATH; pass its path as the argument.")
    return found


def enable_fake_review(config: Path) -> None:
    """Switch `[llm.review]` to a dual fake reviewer by editing only that section."""
    lines = config.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_review = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_review = stripped == "[llm.review]"
        if in_review and stripped.startswith("mode ="):
            out += [
                'mode = "dual"',
                'policy = "human_review"',
                'provider = "fake"',
                'model = "fake-reviewer"',
            ]
            continue
        out.append(line)
    config.write_text("\n".join(out) + "\n", encoding="utf-8")


def calibrate(root: Path, run: Callable[..., subprocess.CompletedProcess[str]]) -> None:
    run("calibrate", "sample", "-w", "an", "--set", "s1", "--size", "4", "--out", "s1.csv")
    with (root / "s1.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    labels = ["HIGH", "MEDIUM", "LOW", "HIGH"]
    for row, label in zip(rows, labels, strict=False):
        row["human_label"] = label
    with (root / "s1_filled.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    run("calibrate", "import", "-w", "an", "--set", "s1", "--labels", "s1_filled.csv")
    # The default minimum (30 labels) protects against quoting figures from tiny sets.
    run("calibrate", "report", "-w", "an", "--set", "s1", "--out", "report.csv", expect=10)
    config = root / "an" / "analyzer.toml"
    text = config.read_text(encoding="utf-8")
    assert "min_labels_report = 30" in text
    config.write_text(text.replace("min_labels_report = 30", "min_labels_report = 3"))
    run("calibrate", "report", "-w", "an", "--set", "s1", "--out", "report.csv")


def main() -> int:
    exe = find_executable(sys.argv[1] if len(sys.argv) > 1 else None)
    with tempfile.TemporaryDirectory(prefix="wca-quickstart-") as tmp:
        root = Path(tmp)

        def run(*args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
            proc = subprocess.run(
                [exe, *args],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            label = "whatsapp-analyzer " + " ".join(args)
            if proc.returncode != expect:
                print(f"FAIL ({proc.returncode}, expected {expect}): {label}")
                print(proc.stdout[-1500:])
                print(proc.stderr[-1500:])
                sys.exit(1)
            print(f"ok   {label}")
            return proc

        version = run("--version").stdout.strip()
        print(f"version: {version}")
        run("doctor")
        run("demo-data", "demo", "--seed", "7", "--conversations", "6")
        run("import", "demo/conversations.csv")  # validate only
        run("import", "demo/conversations.xlsx")
        (root / "mapping.toml").write_text(MAPPING, encoding="utf-8")
        run(
            "import",
            "demo/conversations_custom_columns.csv",
            "--mapping",
            "mapping.toml",
        )
        run("init", "an")
        run("import", "demo/conversations.csv", "-w", "an")
        run("metrics", "run", "-w", "an")
        run("status", "-w", "an")
        run("export", "-w", "an", "--out", "scores.csv")
        run("llm", "prompts")
        run("llm", "run", "-w", "an")
        run("llm", "status", "-w", "an")
        run("llm", "export", "-w", "an", "--out", "llm.csv")

        enable_fake_review(root / "an" / "analyzer.toml")
        run("llm", "review", "-w", "an")
        run("llm", "consolidate", "-w", "an", "--out", "consolidated.csv")

        calibrate(root, run)

        doctor = json.loads(run("--json", "doctor").stdout)
        assert doctor["ok"] is True, doctor
        for name in ("scores.csv", "llm.csv", "consolidated.csv", "report.csv"):
            assert (root / name).stat().st_size > 0, name
        assert (root / "scores.csv.meta.json").exists()
    print("quickstart check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
