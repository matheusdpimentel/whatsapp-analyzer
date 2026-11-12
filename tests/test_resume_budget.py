"""M6-01, resumed runs: attempts recorded at an under-stated rate must count at the applicable
rate toward the budget before anything else is dispatched (offline, real SQLite).

The scenario of the independent audit: a run started with a GPT-6 Luna price snapshot that
had no long-context tier, five billed attempts of 300,000 input and 128,000 output tokens each
were recorded at US$ 0.094 instead of 0.156, the run stopped, and it is resumed with
`--max-cost 1.00`. The synthetic conversations are large (about 270K estimated input tokens
each, the audit's shape) so the local reservation is close to what the mock provider reports.
"""

import csv
import json
import shutil
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from helpers import HEADER, new_workspace, run_cli
from test_provider_runs import mock
from test_providers import openai_body
from whatsapp_analyzer.store.db import connect
from whatsapp_analyzer.workspace import DATABASE_FILE

JOBS = 7
MESSAGES = 80  # the audit's shape: 80 messages of 16,000 characters
CHARS = 16_000
LONG_USAGE = {
    "prompt_tokens": 300_000,
    "completion_tokens": 128_000,
    "total_tokens": 428_000,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
}
SHORT_COST = Decimal("0.094")  # 300K x 0.10 + 128K x 0.50, per million
LONG_COST = Decimal("0.156")  # 300K x 0.20 + 128K x 0.75: the applicable tier (> 272K)
OLD_RATE = Decimal("0.50") / 10**6  # worst rate of the snapshot without a tier
NEW_RATE = Decimal("0.75") / 10**6  # worst rate across both tiers
CONFIG = """
[llm]
provider = "openai"
model = "gpt-6-luna"
max_output_tokens = 128000
[llm.connection]
api_key_env = "TEST_LLM_KEY"
[llm.context]
max_messages = 50
max_context_tokens = 600000
max_message_chars = 16000
[llm.limits]
concurrency = 1
requests_per_minute = 100000
"""
UNTIERED_PRICE = """schema = 1
[[prices]]
provider = "openai"
model = "gpt-6-luna"
currency = "USD"
unit = "per_million_tokens"
input = "0.10"
cache_read = "0.01"
cache_write = "0.125"
output = "0.50"
effective_from = 2026-11-10
retrieved_at = 2026-11-10
source = "the short-context price only, as shipped before the fix"
"""


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace with seven large synthetic conversations of identical size."""
    root = tmp_path_factory.mktemp("resume-template")
    workspace = new_workspace(root)
    source = root / "synthetic.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(HEADER)
        for job in range(JOBS):
            writer.writerow(
                [f"c{job}", "2026-01-05T09:00:00", "owner", "outbound", "Hello", "text", ""]
            )
            for message in range(MESSAGES):
                prefix = f"conversation {job} message {message} "
                text = (prefix + "lorem ipsum dolor sit amet " * CHARS)[:CHARS]
                minute = 10 + message
                writer.writerow(
                    [
                        f"c{job}",
                        f"2026-01-05T09:{minute}:00",
                        "Contact",
                        "inbound",
                        text,
                        "text",
                        "",
                    ]
                )
    import io  # noqa: PLC0415
    from contextlib import redirect_stdout  # noqa: PLC0415

    from whatsapp_analyzer.cli.app import main  # noqa: PLC0415

    with redirect_stdout(io.StringIO()):
        assert main(["--json", "import", str(source), "-w", str(workspace)]) == 0
    return workspace


@pytest.fixture
def workspace(template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test-resume-0123456789")
    copy = tmp_path / "ws"
    shutil.copytree(template, copy)
    (tmp_path / "real.toml").write_text(CONFIG, encoding="utf-8")
    return copy


def reply(usage: dict[str, Any] | None) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda request: httpx2.Response(200, json=openai_body(usage=usage))


def run(
    workspace: Path, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, dict[str, Any]]:
    config = workspace.parent / "real.toml"
    argv = ["llm", "run", "-w", str(workspace), "--config", str(config), *args]
    code, payload = run_cli(argv, capsys)
    return code, payload


def db(workspace: Path) -> sqlite3.Connection:
    return connect(workspace / DATABASE_FILE)


def legacy_run(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, Any] | None = LONG_USAGE,
) -> tuple[Any, int]:
    """Five billed attempts recorded with the untiered snapshot, then the run stops (budget).
    Returns the transport and the reserved tokens of one request."""
    (workspace / "pricing.toml").write_text(UNTIERED_PRICE, encoding="utf-8")
    transport = mock(monkeypatch, reply(usage))
    limit = five_only(workspace, capsys, usage is not None)  # five fit, a sixth does not
    code, payload = run(workspace, capsys, "--max-tokens", str(limit), "--authorize-send", "openai")
    assert code == 7, payload  # budget reached: a resumable stop
    assert len(transport.requests) == 5
    connection = db(workspace)
    reserved = {r[0] for r in connection.execute("SELECT reserved_tokens FROM llm_attempts")}
    snapshot = json.loads(connection.execute("SELECT price_json FROM llm_runs").fetchone()[0])
    connection.close()
    assert len(reserved) == 1 and snapshot["max_input_tokens"] is None  # the legacy state
    (workspace / "pricing.toml").unlink()  # the current table has the long-context tier
    return transport, reserved.pop()


def five_only(workspace: Path, capsys: pytest.CaptureFixture[str], usage: bool = True) -> int:
    """A token limit that lets exactly five attempts through. Each one is charged its reported
    428K tokens, or its reservation when the usage is missing."""
    code, payload = run(workspace, capsys, "--max-tokens", "1000000000")  # not authorized
    assert code == 12
    estimate = payload["error"]["details"]["estimate"]
    reservation = (
        estimate["estimated_input_tokens"] // JOBS + estimate["max_output_tokens_per_request"]
    )
    charge = 428_000 if usage else int(reservation)
    return 5 * charge + int(reservation) - 1


def resume(
    workspace: Path, capsys: pytest.CaptureFixture[str], max_cost: Decimal | str, *extra: str
) -> tuple[int, dict[str, Any]]:
    return run(
        workspace,
        capsys,
        "--max-tokens",
        "1000000000",
        "--max-cost",
        str(max_cost),
        "--authorize-send",
        "openai",
        *extra,
    )


def recorded_costs(workspace: Path) -> list[Decimal | None]:
    connection = db(workspace)
    try:
        rows = connection.execute("SELECT cost FROM llm_attempts ORDER BY id")
        return [None if r[0] is None else Decimal(r[0]) for r in rows]
    finally:
        connection.close()


# The audit's scenario ----------------------------------------------------------------------


def test_audit_scenario_two_new_calls_are_not_dispatched(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, reserved = legacy_run(workspace, capsys, monkeypatch)
    assert recorded_costs(workspace) == [SHORT_COST] * 5  # 0.470 recorded

    code, payload = resume(workspace, capsys, "1.00")
    # Corrected history 0.780 + the next worst case (~0.32) exceeds 1.00: nothing is sent.
    # Before the fix, two calls were dispatched (0.782 recorded, 1.092 applicable).
    assert code == 7, payload
    assert len(transport.requests) == 5
    budget = payload["data"]["budget"]
    assert Decimal(budget["committed_cost"]) == LONG_COST * 5
    assert Decimal(budget["cost_adjustment"]) == (LONG_COST - SHORT_COST) * 5
    # the historical record is untouched and still traceable
    assert recorded_costs(workspace) == [SHORT_COST] * 5
    assert Decimal(payload["data"]["cost"]["known_total"]) == SHORT_COST * 5
    assert reserved * NEW_RATE + LONG_COST * 5 > Decimal(1)


def test_history_already_above_the_budget(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _ = legacy_run(workspace, capsys, monkeypatch)
    code, payload = resume(workspace, capsys, "0.70")  # below the corrected 0.780 already
    assert code == 7 and len(transport.requests) == 5
    assert "max_cost" in payload["data"]["run"]["stop_reason"]


@pytest.mark.parametrize(("slack", "sends"), [(Decimal(0), 1), (Decimal("-0.000001"), 0)])
def test_room_for_exactly_one_call_and_the_exact_boundary(
    slack: Decimal,
    sends: int,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, reserved = legacy_run(workspace, capsys, monkeypatch)
    limit = LONG_COST * 5 + reserved * NEW_RATE + slack  # committed + one worst case
    code, payload = resume(workspace, capsys, limit)
    assert code == 7, payload
    assert len(transport.requests) == 5 + sends
    committed = Decimal(payload["data"]["budget"]["committed_cost"])
    assert committed == LONG_COST * (5 + sends) <= limit


def test_missing_usage_is_charged_at_the_reservation_with_the_applicable_rate(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, reserved = legacy_run(workspace, capsys, monkeypatch, usage=None)
    assert recorded_costs(workspace) == [None] * 5  # unknown, never zero
    code, payload = resume(workspace, capsys, "1.00")
    assert code == 7 and len(transport.requests) == 5
    assert Decimal(payload["data"]["budget"]["committed_cost"]) == reserved * NEW_RATE * 5
    assert payload["data"]["cost"]["known_total"] is None


def test_unknown_reservations_are_kept_at_the_applicable_rate(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, reserved = legacy_run(workspace, capsys, monkeypatch)
    connection = db(workspace)
    with connection:  # one legacy attempt had an unknown outcome
        connection.execute(
            "UPDATE llm_attempts SET billing = 'unknown', cost = NULL, reserved_cost = ? "
            "WHERE id = (SELECT min(id) FROM llm_attempts)",
            (str(reserved * OLD_RATE),),
        )
    connection.close()
    code, payload = resume(workspace, capsys, "10")
    data = payload["data"]
    assert Decimal(data["reservations"]["unknown_billing_cost"]) == reserved * NEW_RATE
    assert data["reservations"]["unknown_billing_tokens"] == reserved
    assert code in {0, 8}, payload
    assert len(transport.requests) == 7  # the two pending jobs, never the unknown one again


def test_mixed_old_and_new_attempts(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _ = legacy_run(workspace, capsys, monkeypatch)
    code, payload = resume(workspace, capsys, "10")
    assert code == 0, payload
    assert len(transport.requests) == 7
    assert recorded_costs(workspace) == [SHORT_COST] * 5 + [LONG_COST] * 2
    assert Decimal(payload["data"]["budget"]["committed_cost"]) == LONG_COST * 7


def test_consecutive_resumes_apply_the_correction_once(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _ = legacy_run(workspace, capsys, monkeypatch)
    first = resume(workspace, capsys, "1.00")[1]["data"]["budget"]
    # an interrupted preparation: planned and upgraded, then refused before sending
    assert run(workspace, capsys, "--max-cost", "1.00")[0] == 12
    third = resume(workspace, capsys, "1.00")[1]["data"]["budget"]
    assert first["committed_cost"] == third["committed_cost"] == str(LONG_COST * 5)
    assert first["cost_adjustment"] == third["cost_adjustment"]
    assert len(transport.requests) == 5


def test_a_correct_snapshot_needs_no_adjustment(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, reply(LONG_USAGE))
    code, payload = run(
        workspace,
        capsys,
        "--max-tokens",
        str(five_only(workspace, capsys)),
        "--authorize-send",
        "openai",
    )
    assert code == 7 and len(transport.requests) == 5
    assert recorded_costs(workspace) == [LONG_COST] * 5  # priced right from the start
    code, payload = resume(workspace, capsys, "10")
    assert code == 0 and payload["data"]["budget"]["cost_adjustment"] == "0"
    assert len(transport.requests) == 7


def test_a_legitimate_price_change_does_not_reprice_the_run(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, reply(LONG_USAGE))
    limit = str(five_only(workspace, capsys))
    assert run(workspace, capsys, "--max-tokens", limit, "--authorize-send", "openai")[0] == 7
    assert len(transport.requests) == 5
    # prices go up later; the run keeps the snapshot it started with
    raised = UNTIERED_PRICE.replace('input = "0.10"', 'input = "9.00"').replace(
        'output = "0.50"', 'output = "9.00"'
    )
    raised += (
        'max_input_tokens = 272000\nlong_input = "18"\nlong_cache_read = "1"\n'
        'long_cache_write = "20"\nlong_output = "18"\n'
    )
    (workspace / "pricing.toml").write_text(raised, encoding="utf-8")
    code, payload = resume(workspace, capsys, "10")
    assert code == 0, payload
    assert payload["data"]["budget"]["cost_adjustment"] == "0"
    assert Decimal(payload["data"]["budget"]["committed_cost"]) == LONG_COST * 7
    assert len(transport.requests) == 7


def test_export_keeps_the_recorded_costs(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_run(workspace, capsys, monkeypatch)
    resume(workspace, capsys, "1.00")
    out = workspace.parent / "llm.csv"
    code, _ = run_cli(["llm", "export", "-w", str(workspace), "--out", str(out)], capsys)
    assert code == 0
    with out.open(encoding="utf-8") as handle:
        costs = sorted(row["cost"] for row in csv.DictReader(handle) if row["cost"])
    assert costs == [str(SHORT_COST)] * 5  # what was recorded, traceable for audit
