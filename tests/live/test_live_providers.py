"""Opt-in smoke test against a real provider (see README.md in this directory).

Skipped unless WCA_LIVE_PROVIDER and WCA_LIVE_AUTHORIZE name the same provider. One synthetic
conversation, one attempt, a hard cost limit of 0.01 USD.
"""

import csv
import json
import os
from pathlib import Path

import pytest

from whatsapp_analyzer.cli.app import main

PROVIDER = os.environ.get("WCA_LIVE_PROVIDER", "")
DEFAULT_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-6-luna"}
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

pytestmark = [
    pytest.mark.live,
    pytest.mark.enable_socket,
    pytest.mark.skipif(
        PROVIDER not in DEFAULT_MODELS or os.environ.get("WCA_LIVE_AUTHORIZE") != PROVIDER,
        reason="live provider tests need WCA_LIVE_PROVIDER and WCA_LIVE_AUTHORIZE",
    ),
]


def cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = main(["--json", *args])
    return code, json.loads(capsys.readouterr().out)


def test_one_synthetic_conversation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace = tmp_path / "ws"
    assert cli(["init", str(workspace)], capsys)[0] == 0
    source = tmp_path / "synthetic.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "conversation",
                "timestamp",
                "sender",
                "direction",
                "text",
                "message_type",
                "media_kind",
            ]
        )
        writer.writerow(
            ["demo", "2026-01-05T10:00:00", "owner", "outbound", "Hi! Free on Friday?", "text", ""]
        )
        writer.writerow(
            [
                "demo",
                "2026-01-05T10:07:00",
                "Demo Contact",
                "inbound",
                "Yes, what time works?",
                "text",
                "",
            ]
        )
    assert cli(["import", str(source), "-w", str(workspace)], capsys)[0] == 0
    config = tmp_path / "live.toml"
    model = os.environ.get("WCA_LIVE_MODEL", DEFAULT_MODELS[PROVIDER])
    config.write_text(
        f'[llm]\nprovider = "{PROVIDER}"\nmodel = "{model}"\n'
        f'[llm.connection]\napi_key_env = "{KEY_ENV[PROVIDER]}"\n',
        encoding="utf-8",
    )
    code, payload = cli(
        [
            "llm",
            "run",
            "-w",
            str(workspace),
            "--config",
            str(config),
            "--max-cost",
            "0.01",
            "--max-attempts",
            "1",
            "--authorize-send",
            PROVIDER,
        ],
        capsys,
    )
    assert code in {0, 8}, payload
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["destination"] == PROVIDER
