"""The persistent LLM runner with scripted fake providers. All data is synthetic; no network."""

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from helpers import exchange, new_workspace, row, run_cli, write_csv
from whatsapp_analyzer.config import LimitSettings, Settings
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.llm.call import build_request
from whatsapp_analyzer.llm.context import build_context
from whatsapp_analyzer.llm.fake import FakeProvider, FakeScenario
from whatsapp_analyzer.llm.pricing import parse_price_table
from whatsapp_analyzer.llm.prompts import find_template
from whatsapp_analyzer.llm.types import (
    ErrorKind,
    FinishReason,
    LLMRequest,
    LLMResult,
    ProviderError,
    Usage,
)
from whatsapp_analyzer.metrics.raw import StoredMessage
from whatsapp_analyzer.runner import engine as engine_module
from whatsapp_analyzer.runner.engine import EngineOptions
from whatsapp_analyzer.runner.model import FakeClock, Hooks, JobStatus, RunStatus
from whatsapp_analyzer.runner.plan import RunConfig, RunConfigChangedError
from whatsapp_analyzer.runner.reports import export_llm_run, run_report
from whatsapp_analyzer.runner.service import (
    BudgetUnverifiableError,
    ConflictingOptionsError,
    OpenedRun,
    OpenRequest,
    execute,
    open_run,
)
from whatsapp_analyzer.schema import Direction, MessageType
from whatsapp_analyzer.store.db import connect
from whatsapp_analyzer.workspace import DATABASE_FILE, Workspace

SECRET = "Synthetic private phrase 5d1e"
SETTINGS = Settings()
COMPLETE_PRICE = parse_price_table(
    {
        "schema": 1,
        "prices": [
            {
                "provider": "fake",
                "model": "fake-classifier",
                "currency": "USD",
                "unit": "per_million_tokens",
                "input": "1.00",
                "cache_read": "0.10",
                "cache_write": "1.25",
                "output": "4.00",
                "effective_from": date(2026, 1, 1),
                "source": "test",
            }
        ],
    },
    "test",
).prices[0]


class Crash(BaseException):
    """Simulates the process dying: nothing in the runner handles it."""


Action = Callable[[LLMRequest], LLMResult] | ProviderError | type[Crash] | str


class ScriptedProvider:
    """Answers like FakeProvider unless a script says otherwise for the n-th call of a
    request: a ProviderError to raise, Crash to raise, or "hang"."""

    def __init__(self, script: dict[int, Action] | None = None, per_request: bool = False) -> None:
        self.inner = FakeProvider()
        self.script = script or {}
        self.per_request = per_request
        self.calls = 0
        self.calls_by_request: dict[str, int] = {}
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def name(self) -> str:
        return "fake"

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        seen = self.calls_by_request.get(request.request_id, 0) + 1
        self.calls_by_request[request.request_id] = seen
        index = seen if self.per_request else self.calls
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            action = self.script.get(index)
            if action == "hang":
                await asyncio.Event().wait()
            if action is Crash:
                raise Crash
            if isinstance(action, ProviderError):
                raise action
            if callable(action) and not isinstance(action, type):
                return action(request)
            return await self.inner.complete(request)
        finally:
            self.in_flight -= 1


def populated(tmp_path: Path, capsys: pytest.CaptureFixture[str], count: int = 4) -> Path:
    workspace = new_workspace(tmp_path)
    rows = []
    for index in range(count):
        key = f"chat{index:02d}"
        rows += [
            row(key, f"2026-01-0{1 + index % 8}T10:00:00", "outbound", f"Hello number {index}"),
            row(
                key,
                f"2026-01-0{1 + index % 8}T10:05:00",
                "inbound",
                f"{SECRET} {index}?",
                sender=f"Bruna Privada {index}",
            ),
        ]
    run_cli(["import", str(write_csv(tmp_path / "in.csv", rows)), "-w", str(workspace)], capsys)
    return workspace


def cfg(price: object = None, **limits: object) -> RunConfig:
    base = RunConfig(
        provider="fake",
        model="fake-classifier",
        template=find_template(None),
        context=SETTINGS.llm.context,
        ignored_texts=SETTINGS.metrics.ignored_texts,
        limits=replace(LimitSettings(requests_per_minute=None), **limits),  # type: ignore[arg-type]
        price=price,  # type: ignore[arg-type]
    )
    return base


def go(
    connection: sqlite3.Connection,
    provider: object,
    run_config: RunConfig | None = None,
    request: OpenRequest | None = None,
    clock: FakeClock | None = None,
    options: EngineOptions | None = None,
) -> tuple[OpenedRun, RunStatus | None]:
    opened = open_run(connection, run_config or cfg(), request or OpenRequest())
    if opened.mode not in {"new", "resumed"}:
        return opened, None
    options = replace(options or EngineOptions(), resend_job_ids=opened.resend_job_ids)
    status = asyncio.run(execute(connection, opened, provider, clock or FakeClock(), options))  # type: ignore[arg-type]
    return opened, status


def jobs(connection: sqlite3.Connection, run_id: int) -> dict[str, int]:
    return dict(
        connection.execute(
            "SELECT status, count(*) FROM llm_jobs WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
    )


def db(workspace: Path) -> sqlite3.Connection:
    return connect(workspace / DATABASE_FILE)


# Basic run, up to date, reuse ------------------------------------------------------------


def test_run_completes_and_is_then_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys))
    provider = ScriptedProvider()
    opened, status = go(connection, provider)
    assert (opened.mode, status) == ("new", RunStatus.COMPLETED)
    assert jobs(connection, opened.run_id) == {"succeeded": 4}
    assert provider.calls == 4
    again, status = go(connection, provider)
    assert (again.mode, again.run_id, status) == ("up_to_date", opened.run_id, None)
    assert provider.calls == 4


def test_force_reuses_results_without_copying_billing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys))
    go(connection, ScriptedProvider(), cfg(price=COMPLETE_PRICE))
    provider = ScriptedProvider()
    opened, status = go(connection, provider, cfg(price=COMPLETE_PRICE), OpenRequest(force=True))
    assert status is RunStatus.COMPLETED and provider.calls == 0
    assert jobs(connection, opened.run_id) == {"reused": 4}
    attempts = connection.execute(
        "SELECT count(*) FROM llm_attempts a JOIN llm_jobs j ON j.id = a.job_id WHERE j.run_id = ?",
        (opened.run_id,),
    ).fetchone()[0]
    assert attempts == 0  # no new spend recorded for reused results
    report = run_report(connection, opened.run_id)
    assert report["cost"]["known_total"] is None  # type: ignore[index]


def test_no_reuse_calls_again_only_in_a_run_that_executes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys))
    go(connection, ScriptedProvider())
    provider = ScriptedProvider()
    opened = open_run(connection, cfg(), OpenRequest())
    assert opened.mode == "up_to_date"  # --no-reuse alone does not create a new run
    _, status = go(
        connection, provider, request=OpenRequest(force=True), options=EngineOptions(reuse=False)
    )
    assert status is RunStatus.COMPLETED and provider.calls == 4


def test_identical_requests_in_one_run_are_sent_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = new_workspace(tmp_path)
    rows = []
    for key in ("first", "second"):
        rows += [
            row(key, "2026-01-05T10:00:00", "outbound", "Hello"),
            row(key, "2026-01-05T10:05:00", "inbound", "Sure, thanks?", sender="Same Person"),
        ]
    run_cli(["import", str(write_csv(tmp_path / "in.csv", rows)), "-w", str(workspace)], capsys)
    connection = db(workspace)
    provider = ScriptedProvider()
    opened, status = go(connection, provider, cfg(concurrency=4))
    assert status is RunStatus.COMPLETED and provider.calls == 1
    assert jobs(connection, opened.run_id) == {"succeeded": 1, "reused": 1}


# Scope and identity -----------------------------------------------------------------------


def test_scope_is_frozen_against_later_imports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=2)
    connection = db(workspace)
    with pytest.raises(Crash):
        go(connection, ScriptedProvider({2: Crash}), cfg(concurrency=1))
    planned = connection.execute(
        "SELECT version_id FROM llm_jobs WHERE run_id = 1 ORDER BY id"
    ).fetchall()
    # chat00 gets a new version: the existing run keeps the version it planned.
    rows = [
        row("chat00", "2026-01-01T10:00:00", "outbound", "Hello number 0"),
        row("chat00", "2026-01-01T10:05:00", "inbound", f"{SECRET} 0?", sender="Bruna Privada 0"),
        *exchange("chat00", 20),
    ]
    run_cli(["import", str(write_csv(tmp_path / "more.csv", rows)), "-w", str(workspace)], capsys)
    resumed, status = go(
        connection,
        ScriptedProvider(),
        cfg(concurrency=1),
        OpenRequest(run_id=1, resend_unknown=True),
    )
    assert resumed.run_id == 1 and status is RunStatus.COMPLETED
    assert (
        connection.execute(
            "SELECT version_id FROM llm_jobs WHERE run_id = 1 ORDER BY id"
        ).fetchall()
        == planned
    )
    fresh, _ = go(connection, ScriptedProvider(), cfg(concurrency=1))
    assert fresh.run_id == 2  # the new scope (new version of chat00) is a new run
    versions = {
        r[0] for r in connection.execute("SELECT version_id FROM llm_jobs WHERE run_id = 2")
    }
    assert versions != {r[0] for r in planned}


def test_request_id_covers_generation_parameters() -> None:
    template = find_template(None)
    longer = replace(template, max_output_tokens=template.max_output_tokens + 1)
    assert cfg().run_key() != replace(cfg(), template=longer).run_key()

    context = build_context(
        [
            StoredMessage(
                0, datetime(2026, 1, 1), "x", Direction.INBOUND, "hi", MessageType.TEXT, None
            )
        ],
        SETTINGS.llm.context,
        is_group=False,
        ignored_texts=(),
    )
    a = build_request(template, context, "m")[1]
    b = build_request(longer, context, "m")[1]
    c = build_request(template, context, "other-model")[1]
    assert len({a.request_id, b.request_id, c.request_id}) == 3


def test_resuming_with_a_different_configuration_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys))
    go(connection, ScriptedProvider())
    other = replace(cfg(), model="another-model")
    with pytest.raises(RunConfigChangedError):
        open_run(connection, other, OpenRequest(run_id=1))


# Retries, unknown outcomes and reconciliation ----------------------------------------------


def test_rate_limit_is_retried_after_backoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    clock = FakeClock()
    provider = ScriptedProvider({1: ProviderError(ErrorKind.RATE_LIMITED, retry_after_seconds=7)})
    _, status = go(connection, provider, cfg(concurrency=1), clock=clock)
    assert status is RunStatus.COMPLETED and provider.calls == 2
    assert max(clock.slept) >= 7
    billing = [r[0] for r in connection.execute("SELECT billing FROM llm_attempts ORDER BY id")]
    assert billing == ["rejected", "billed"]


def test_safe_retries_stop_at_max_attempts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    overloaded = ProviderError(ErrorKind.OVERLOADED)
    provider = ScriptedProvider({1: overloaded, 2: overloaded, 3: overloaded})
    opened, status = go(connection, provider, cfg(max_attempts=3))
    assert status is RunStatus.COMPLETED_WITH_FAILURES and provider.calls == 3
    assert jobs(connection, opened.run_id) == {"failed": 1}


def test_network_error_known_not_sent_is_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    provider = ScriptedProvider({1: ProviderError(ErrorKind.NETWORK, request_sent=False)})
    _, status = go(connection, provider)
    assert status is RunStatus.COMPLETED and provider.calls == 2
    assert (
        connection.execute("SELECT billing FROM llm_attempts ORDER BY id").fetchall()[0][0]
        == "not_sent"
    )


@pytest.mark.parametrize(
    "kind",
    [
        ErrorKind.TIMEOUT,
        ErrorKind.NETWORK,
        ErrorKind.SERVER_ERROR,
        ErrorKind.MALFORMED_PROVIDER_RESPONSE,
    ],
)
def test_unknown_outcomes_await_reconciliation_and_are_never_resent_automatically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: ErrorKind
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    provider = ScriptedProvider({1: ProviderError(kind)})
    opened, status = go(connection, provider)
    assert status is RunStatus.COMPLETED_WITH_FAILURES and provider.calls == 1
    assert jobs(connection, opened.run_id) == {"awaiting_reconciliation": 1}
    # Running again, or retrying failed jobs, never sends it.
    for request in (OpenRequest(), OpenRequest(retry_failed=True)):
        again, _ = go(connection, provider, request=request)
        assert again.mode in {"unchanged", "resumed"}
    assert provider.calls == 1
    # Only --resend-unknown does; the first attempt is kept for audit.
    _, status = go(connection, provider, request=OpenRequest(resend_unknown=True))
    assert status is RunStatus.COMPLETED and provider.calls == 2
    billing = [r[0] for r in connection.execute("SELECT billing FROM llm_attempts ORDER BY id")]
    assert billing == ["unknown", "billed"]


def test_retry_failed_resends_failed_jobs_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=2))

    def invalid(request: LLMRequest) -> LLMResult:
        return LLMResult("not json", FinishReason.STOP, Usage(1, 0, 0, 1))

    provider = ScriptedProvider({1: invalid, 2: ProviderError(ErrorKind.TIMEOUT)})
    opened, _ = go(connection, provider, cfg(concurrency=1))
    assert jobs(connection, opened.run_id) == {"failed": 1, "awaiting_reconciliation": 1}
    go(connection, provider, request=OpenRequest(retry_failed=True))
    assert provider.calls == 3
    assert jobs(connection, opened.run_id) == {"succeeded": 1, "awaiting_reconciliation": 1}


def test_account_errors_pause_the_run_and_resume_later(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=4))
    provider = ScriptedProvider({2: ProviderError(ErrorKind.INSUFFICIENT_CREDITS)})
    opened, status = go(connection, provider, cfg(concurrency=1))
    assert status is RunStatus.PAUSED
    run = connection.execute("SELECT stop_reason FROM llm_runs").fetchone()[0]
    assert run == "ACCOUNT_ERROR:INSUFFICIENT_CREDITS"
    assert jobs(connection, opened.run_id) == {"succeeded": 1, "pending": 3}
    _, status = go(connection, provider, cfg(concurrency=1))
    assert status is RunStatus.COMPLETED and jobs(connection, opened.run_id) == {"succeeded": 4}


# Budgets, reservations and limits ------------------------------------------------------------


def reserved_per_attempt(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT max(reserved_tokens) FROM llm_attempts").fetchone()[0])


def test_token_budget_stops_before_exceeding_and_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=4))
    go(connection, ScriptedProvider(), cfg(), OpenRequest(keys=("chat00",)))
    per = reserved_per_attempt(connection)
    budget = per * 2 + per // 2
    opened, status = go(
        connection,
        ScriptedProvider(),
        cfg(max_tokens=budget),
        OpenRequest(force=True),
        options=EngineOptions(reuse=False),
    )
    assert status is RunStatus.BUDGET_REACHED
    report = run_report(connection, opened.run_id)
    committed = report["budget"]["committed_tokens"]  # type: ignore[index]
    assert isinstance(committed, int) and committed <= budget
    done = jobs(connection, opened.run_id)
    assert done.get("succeeded", 0) >= 2 and done.get("pending", 0) >= 1
    # Resuming without a flag keeps the budget (H2) ...
    _, status = go(connection, ScriptedProvider(), cfg(), OpenRequest(run_id=opened.run_id))
    assert status is RunStatus.BUDGET_REACHED
    stored = json.loads(
        connection.execute(
            "SELECT limits_json FROM llm_runs WHERE id = ?", (opened.run_id,)
        ).fetchone()[0]
    )
    assert stored["max_tokens"] == budget
    # ... and only an explicit override raises it.
    raised = OpenRequest(run_id=opened.run_id, limit_overrides=(("max_tokens", budget * 10),))
    _, status = go(connection, ScriptedProvider(), cfg(), raised)
    assert status is RunStatus.COMPLETED


def test_reservations_bound_concurrent_dispatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=6))
    go(connection, ScriptedProvider(), cfg(), OpenRequest(keys=("chat00",)))
    per = reserved_per_attempt(connection)
    provider = ScriptedProvider()
    # Room for two worst cases only, with 4 slots: never more than 2 reservations at once.
    go(
        connection,
        provider,
        cfg(concurrency=4, max_tokens=per * 2 + 1),
        OpenRequest(force=True),
        options=EngineOptions(reuse=False),
    )
    assert provider.max_in_flight <= 2


def test_unknown_billing_keeps_its_reservation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    opened, _ = go(
        connection,
        ScriptedProvider({1: ProviderError(ErrorKind.TIMEOUT)}),
        cfg(price=COMPLETE_PRICE),
    )
    report = run_report(connection, opened.run_id)
    reserved = connection.execute(
        "SELECT reserved_tokens, reserved_cost FROM llm_attempts"
    ).fetchone()
    assert report["reservations"]["unknown_billing_tokens"] == reserved[0]  # type: ignore[index]
    assert report["reservations"]["unknown_billing_cost"] == reserved[1]  # type: ignore[index]
    assert report["budget"]["committed_tokens"] == reserved[0]  # type: ignore[index]


def test_max_cost_requires_a_complete_price(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    with pytest.raises(BudgetUnverifiableError):
        open_run(connection, cfg(max_cost=Decimal("1.00")), OpenRequest())
    incomplete = replace(COMPLETE_PRICE, cache_write=None)
    with pytest.raises(BudgetUnverifiableError):
        open_run(connection, cfg(price=incomplete, max_cost=Decimal("1.00")), OpenRequest())
    _, status = go(
        connection, ScriptedProvider(), cfg(price=COMPLETE_PRICE, max_cost=Decimal("1.00"))
    )
    assert status is RunStatus.COMPLETED


def test_cost_budget_uses_worst_case_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=3))
    go(connection, ScriptedProvider(), cfg(price=COMPLETE_PRICE), OpenRequest(keys=("chat00",)))
    one = Decimal(connection.execute("SELECT reserved_cost FROM llm_attempts").fetchone()[0])
    assert one == Decimal(reserved_per_attempt(connection)) * Decimal("4.00") / Decimal(1_000_000)
    opened, status = go(
        connection,
        ScriptedProvider(),
        cfg(price=COMPLETE_PRICE, max_cost=one * Decimal("1.5")),
        OpenRequest(force=True),
        options=EngineOptions(reuse=False),
    )
    assert status is RunStatus.BUDGET_REACHED
    spent = run_report(connection, opened.run_id)["budget"]["committed_cost"]  # type: ignore[index]
    assert Decimal(spent) <= one * Decimal("1.5")


def test_unknown_usage_counts_are_never_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))

    def partial_usage(request: LLMRequest) -> LLMResult:
        ids = [
            line.split("]")[0][1:]
            for line in request.messages[0].content.splitlines()
            if "CONTACT:" in line
        ]
        text = json.dumps(
            {"level": "MEDIUM", "reasons": ["CONFIRMS_OR_AGREES"], "evidence": ids[:1]}
        )
        return LLMResult(
            text,
            FinishReason.STOP,
            Usage(input_uncached=100, input_cache_read=None, input_cache_write=0, output_total=10),
        )

    opened, status = go(connection, ScriptedProvider({1: partial_usage}))
    assert status is RunStatus.COMPLETED
    report = run_report(connection, opened.run_id)
    assert report["usage"]["reported"]["attempts_with_unknown_counts"] == 1  # type: ignore[index]
    assert report["usage"]["reported"]["input_uncached"] is None  # type: ignore[index]
    assert report["budget"]["committed_tokens"] == reserved_per_attempt(connection)  # type: ignore[index]


def test_concurrency_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connection = db(populated(tmp_path, capsys, count=8))
    provider = ScriptedProvider()
    _, status = go(connection, provider, cfg(concurrency=3))
    assert status is RunStatus.COMPLETED and provider.max_in_flight == 3


def test_requests_per_minute(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connection = db(populated(tmp_path, capsys, count=5))
    clock = FakeClock()
    _, status = go(connection, ScriptedProvider(), cfg(requests_per_minute=2), clock=clock)
    assert status is RunStatus.COMPLETED
    assert clock.time >= 89  # 2 at once, then one every 30 s


def test_tokens_per_minute(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    connection = db(populated(tmp_path, capsys, count=3))
    go(connection, ScriptedProvider(), cfg(), OpenRequest(keys=("chat00",)))
    per = reserved_per_attempt(connection)
    clock = FakeClock()
    _, status = go(
        connection,
        ScriptedProvider(),
        cfg(tokens_per_minute=per),
        OpenRequest(force=True),
        clock=clock,
        options=EngineOptions(reuse=False),
    )
    assert status is RunStatus.COMPLETED
    assert clock.time >= 2 * 60 * 0.9


# Checkpoints and crashes -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "expected_job", "provider_calls"),
    [
        ("before_checkpoint", "pending", 0),
        ("after_checkpoint", "awaiting_reconciliation", 0),
        ("during_call", "awaiting_reconciliation", 1),
        ("after_provider_return", "awaiting_reconciliation", 1),
        ("after_result_commit", "succeeded", 1),
    ],
)
def test_crash_points(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    point: str,
    expected_job: str,
    provider_calls: int,
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))

    def crash(job_id: int) -> None:
        raise Crash

    hooks = Hooks(**{point: crash}) if point != "during_call" else Hooks()
    provider = ScriptedProvider({1: Crash} if point == "during_call" else {})
    with pytest.raises(Crash):
        go(connection, provider, options=EngineOptions(hooks=hooks))
    assert provider.calls == provider_calls
    # A new process: recovery runs when the next writer opens the run.
    later = db(tmp_path / "ws")
    reopened = open_run(later, cfg(), OpenRequest())
    assert jobs(later, reopened.run_id) == {expected_job: 1}
    if expected_job == "awaiting_reconciliation":
        phase = later.execute("SELECT phase, billing FROM llm_attempts").fetchone()
        assert tuple(phase) == ("abandoned", "unknown")
        assert reopened.mode in {"unchanged", "resumed"}
    resumed_provider = ScriptedProvider()
    status = (
        asyncio.run(execute(later, reopened, resumed_provider, FakeClock()))
        if reopened.mode == "resumed"
        else None
    )
    if expected_job == "pending":
        assert status is RunStatus.COMPLETED and resumed_provider.calls == 1
    else:
        assert resumed_provider.calls == 0  # confirmed results are not lost; unknown ones wait


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_killed_process_leaves_a_reconcilable_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=1)
    script = f"""
import asyncio, sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_runner import ScriptedProvider, cfg
from whatsapp_analyzer.runner.service import OpenRequest, open_run, execute
from whatsapp_analyzer.runner.model import FakeClock
from whatsapp_analyzer.store.db import connect
c = connect(__import__("pathlib").Path({str(workspace / DATABASE_FILE)!r}))
opened = open_run(c, cfg(), OpenRequest())
asyncio.run(execute(c, opened, ScriptedProvider({{1: "hang"}}), FakeClock()))
"""
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                probe = sqlite3.connect(workspace / DATABASE_FILE)
                found = probe.execute(
                    "SELECT count(*) FROM llm_attempts WHERE phase = 'dispatched'"
                ).fetchone()[0]
                probe.close()
            except sqlite3.Error:
                continue
            if found:
                break
        os.kill(process.pid, signal.SIGKILL)
    finally:
        process.wait(timeout=30)
    connection = db(workspace)
    reopened = open_run(connection, cfg(), OpenRequest())
    assert reopened.recovered_attempts == 1
    assert jobs(connection, reopened.run_id) == {"awaiting_reconciliation": 1}
    assert connection.execute("SELECT status, stop_reason FROM llm_runs").fetchone()[1] == "CRASH"


def test_cancellation_marks_in_flight_attempts_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=2))
    opened = open_run(connection, cfg(concurrency=2), OpenRequest())

    async def scenario() -> None:
        task = asyncio.create_task(
            execute(connection, opened, ScriptedProvider({1: "hang", 2: "hang"}), FakeClock())
        )
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())
    assert (
        connection.execute("SELECT status, stop_reason FROM llm_runs").fetchone()[0]
        == "interrupted"
    )
    assert jobs(connection, opened.run_id) == {"awaiting_reconciliation": 2}
    assert {r[0] for r in connection.execute("SELECT billing FROM llm_attempts")} == {"unknown"}


# Export, privacy, CLI ----------------------------------------------------------------------


def test_export_during_a_run_is_a_consistent_partial_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=4)
    writer = db(workspace)
    reader = db(workspace)
    snapshots: list[dict[str, object]] = []

    def export_now() -> None:
        result = export_llm_run(reader, None, tmp_path / f"p{len(snapshots)}.csv")
        snapshots.append(result.metadata)

    _, status = go(
        writer, ScriptedProvider(), cfg(concurrency=1), options=EngineOptions(progress=export_now)
    )
    assert status is RunStatus.COMPLETED
    assert any(s["run"]["partial"] for s in snapshots)  # type: ignore[index]
    for snapshot in snapshots:
        counts = snapshot["jobs"]
        assert isinstance(counts, dict)
        counted = sum(counts.values())
        assert counted == snapshot["rows"] == 4
    final = export_llm_run(reader, None, tmp_path / "final.csv")
    assert final.metadata["run"]["partial"] is False  # type: ignore[index]


def test_export_marks_reconciliation_and_reads_one_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=2)
    writer = db(workspace)
    go(writer, ScriptedProvider({1: ProviderError(ErrorKind.TIMEOUT)}), cfg(concurrency=1))
    reader = db(workspace)

    def concurrent() -> None:
        go(writer, ScriptedProvider(), cfg(concurrency=1), OpenRequest(resend_unknown=True))

    result = export_llm_run(reader, None, tmp_path / "out.csv", on_snapshot=concurrent)
    assert result.metadata["awaiting_reconciliation_rows"] == 1
    text = (tmp_path / "out.csv").read_text(encoding="utf-8")
    assert ",awaiting_reconciliation,true," in text
    after = export_llm_run(reader, None, tmp_path / "after.csv")
    assert after.metadata["awaiting_reconciliation_rows"] == 0


def test_runner_tables_events_and_exports_hold_no_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=3)
    connection = db(workspace)
    events: list[dict[str, object]] = []
    go(
        connection,
        ScriptedProvider({2: ProviderError(ErrorKind.TIMEOUT)}),
        options=EngineOptions(events=events.append),
    )
    export_llm_run(connection, None, tmp_path / "x.csv")
    dumped = [
        json.dumps(events),
        (tmp_path / "x.csv").read_text(encoding="utf-8"),
        (tmp_path / "x.csv.meta.json").read_text(encoding="utf-8"),
        json.dumps(run_report(connection)),
    ]
    for table in ("llm_runs", "llm_jobs", "llm_attempts"):
        dumped.append(
            json.dumps([list(r) for r in connection.execute(f"SELECT * FROM {table}")], default=str)
        )
    blob = "\n".join(dumped)
    for forbidden in (SECRET, "Bruna", "Privada", "Hello number", "<conversation>"):
        assert forbidden not in blob


def test_cli_run_status_export_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=2)
    events = tmp_path / "events.ndjson"
    code, payload = run_cli(["llm", "run", "-w", str(workspace), "--events", str(events)], capsys)
    assert code == ExitCode.OK and payload["command"] == "llm run"
    assert all(json.loads(line)["run_id"] == 1 for line in events.read_text().splitlines())
    code, payload = run_cli(["llm", "run", "-w", str(workspace)], capsys)
    assert code == ExitCode.OK and payload["data"]["mode"] == "up_to_date"  # type: ignore[index]
    code, payload = run_cli(
        [
            "llm",
            "run",
            "-w",
            str(workspace),
            "--force",
            "--no-reuse",
            "--fake-scenario",
            "error:TIMEOUT",
        ],
        capsys,
    )
    assert code == ExitCode.COMPLETED_WITH_FAILURES
    code, payload = run_cli(
        [
            "llm",
            "run",
            "-w",
            str(workspace),
            "--force",
            "--no-reuse",
            "--fake-scenario",
            "error:AUTHENTICATION",
        ],
        capsys,
    )
    assert code == ExitCode.PROVIDER_ACCOUNT_ERROR
    code, payload = run_cli(
        ["llm", "run", "-w", str(workspace), "--force", "--no-reuse", "--max-tokens", "10"], capsys
    )
    assert code == ExitCode.BUDGET_REACHED
    (workspace / "pricing.toml").write_text(  # a newer entry without cache rates
        'schema = 1\n[[prices]]\nprovider = "fake"\nmodel = "fake-classifier"\ncurrency = "USD"\n'
        'unit = "per_million_tokens"\ninput = "1"\noutput = "2"\n'
        'effective_from = 2026-01-02\nsource = "local test"\n',
        encoding="utf-8",
    )
    code, payload = run_cli(
        ["llm", "run", "-w", str(workspace), "--force", "--max-cost", "1"], capsys
    )
    assert (code, payload["error"]["code"]) == (ExitCode.CONFIG_ERROR, "BUDGET_UNVERIFIABLE")  # type: ignore[index]
    (workspace / "pricing.toml").unlink()
    code, payload = run_cli(["llm", "status", "-w", str(workspace)], capsys)
    assert code == ExitCode.OK and payload["data"]["run"]["status"] == "budget_reached"  # type: ignore[index]
    code, payload = run_cli(
        ["llm", "export", "-w", str(workspace), "--run", "1", "--out", str(tmp_path / "o.csv")],
        capsys,
    )
    assert code == ExitCode.OK and payload["data"]["rows"] == 2  # type: ignore[index]
    code, payload = run_cli(
        ["llm", "run", "-w", str(workspace), "--fake-scenario", "nonsense"], capsys
    )
    assert code == ExitCode.USAGE_ERROR


def test_two_runners_cannot_share_a_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=1)
    with Workspace(workspace).lock():
        code, payload = run_cli(["llm", "run", "-w", str(workspace)], capsys)
    assert (code, payload["error"]["code"]) == (ExitCode.USAGE_ERROR, "WORKSPACE_BUSY")  # type: ignore[index]


def test_two_runner_processes_are_serialized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=1)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nfrom pathlib import Path\n"
            "from whatsapp_analyzer.workspace import Workspace\n"
            f"lock = Workspace(Path({str(workspace)!r})).lock()\n"
            "lock.__enter__()\n"
            "print('locked', flush=True)\ntime.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        code, payload = run_cli(["llm", "run", "-w", str(workspace)], capsys)
        assert payload["error"]["code"] == "WORKSPACE_BUSY"  # type: ignore[index]
    finally:
        holder.kill()
        holder.wait(timeout=30)
    code, _ = run_cli(["llm", "run", "-w", str(workspace)], capsys)
    assert code == ExitCode.OK


# Regressions from the internal review -------------------------------------------------------


def twins(tmp_path: Path, capsys: pytest.CaptureFixture[str], count: int = 2) -> sqlite3.Connection:
    """`count` conversations whose masked content, and so request id, is identical."""
    workspace = new_workspace(tmp_path)
    rows = []
    for index in range(count):
        key = f"twin{index}"
        rows += [
            row(key, "2026-01-05T10:00:00", "outbound", "Hello"),
            row(key, "2026-01-05T10:05:00", "inbound", "Sure, thanks?", sender="Same Person"),
        ]
    run_cli(["import", str(write_csv(tmp_path / "in.csv", rows)), "-w", str(workspace)], capsys)
    return db(workspace)


@pytest.mark.parametrize("concurrency", [1, 4])
def test_duplicate_of_an_unknown_outcome_is_never_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], concurrency: int
) -> None:
    connection = twins(tmp_path, capsys)
    provider = ScriptedProvider({1: ProviderError(ErrorKind.TIMEOUT)}, per_request=True)
    opened, status = go(connection, provider, cfg(concurrency=concurrency))
    assert provider.calls == 1 and status is RunStatus.COMPLETED_WITH_FAILURES
    assert jobs(connection, opened.run_id) == {"awaiting_reconciliation": 2}
    kinds = {r[0] for r in connection.execute("SELECT error_kind FROM llm_jobs")}
    assert kinds == {"TIMEOUT", "DUPLICATE_OF_UNKNOWN"}
    # A new run of the same request is not sent either, even with --force --no-reuse.
    _, _ = go(
        connection, provider, request=OpenRequest(force=True), options=EngineOptions(reuse=False)
    )
    assert provider.calls == 1
    # --resend-unknown sends it once; the twin then reuses the result.
    _, status = go(
        connection,
        provider,
        request=OpenRequest(run_id=opened.run_id, resend_unknown=True),
    )
    assert provider.calls == 2 and status is RunStatus.COMPLETED
    assert jobs(connection, opened.run_id) == {"succeeded": 1, "reused": 1}


def test_duplicate_of_a_billed_failure_is_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = twins(tmp_path, capsys)

    def invalid(request: LLMRequest) -> LLMResult:
        return LLMResult("not json", FinishReason.STOP, Usage(1, 0, 0, 1))

    provider = ScriptedProvider({1: invalid}, per_request=True)
    opened, _ = go(connection, provider, cfg(concurrency=1))
    assert provider.calls == 1
    assert jobs(connection, opened.run_id) == {"failed": 2}
    assert {
        r[0]
        for r in connection.execute("SELECT error_kind FROM llm_jobs WHERE error_kind IS NOT NULL")
    } == {"DUPLICATE_OF_FAILED"}


def test_duplicate_waits_for_a_scheduled_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = twins(tmp_path, capsys)
    clock = FakeClock()
    provider = ScriptedProvider(
        {1: ProviderError(ErrorKind.RATE_LIMITED, retry_after_seconds=5)}, per_request=True
    )
    opened, status = go(connection, provider, cfg(concurrency=2), clock=clock)
    assert status is RunStatus.COMPLETED and provider.calls == 2  # the retry, never the twin
    assert jobs(connection, opened.run_id) == {"succeeded": 1, "reused": 1}


def test_an_unexpected_task_failure_stops_dispatching(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=8))
    provider = ScriptedProvider()
    seen: list[int] = []

    def fail_first(job_id: int) -> None:
        seen.append(job_id)
        if len(seen) == 1:
            raise RuntimeError("recording failed")

    with pytest.raises(RuntimeError):
        go(
            connection,
            provider,
            cfg(concurrency=2),
            options=EngineOptions(hooks=Hooks(after_provider_return=fail_first)),
        )
    assert provider.calls <= 3  # at most the attempts already in flight, not the whole queue
    run = connection.execute("SELECT status, stop_reason FROM llm_runs").fetchone()
    assert tuple(run) == ("interrupted", "INTERNAL_ERROR")
    assert (
        connection.execute(
            "SELECT count(*) FROM llm_attempts WHERE phase = 'dispatched'"
        ).fetchone()[0]
        == 0
    )


def test_conflicting_run_options_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    go(connection, ScriptedProvider())
    with pytest.raises(ConflictingOptionsError):
        open_run(connection, cfg(), OpenRequest(run_id=1, force=True))
    with pytest.raises(ConflictingOptionsError):
        open_run(connection, cfg(), OpenRequest(run_id=1, keys=("chat00",)))


def test_incomplete_price_keeps_costs_unknown_not_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=2))
    incomplete = replace(COMPLETE_PRICE, cache_write=None)
    opened, _ = go(
        connection,
        ScriptedProvider({1: ProviderError(ErrorKind.TIMEOUT)}),
        cfg(price=incomplete, concurrency=1),
    )
    report = run_report(connection, opened.run_id)
    assert report["reservations"]["unknown_billing_cost"] is None  # type: ignore[index]
    assert report["budget"]["committed_cost"] is None  # type: ignore[index]


def test_live_status_separates_in_flight_from_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=1)
    writer, reader = db(workspace), db(workspace)
    seen: dict[str, object] = {}

    def look(job_id: int) -> None:
        reservations = run_report(reader)["reservations"]
        assert isinstance(reservations, dict)
        seen.update(reservations)

    go(writer, ScriptedProvider(), options=EngineOptions(hooks=Hooks(after_checkpoint=look)))
    assert seen["in_flight_tokens"] and seen["unknown_billing_tokens"] == 0


def test_demo_scenario_results_are_never_reused_by_normal_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    review = FakeProvider(FakeScenario.REVIEW)
    go(connection, review, replace(cfg(), provider=review.name))
    normal = ScriptedProvider()
    opened, _ = go(connection, normal)
    assert normal.calls == 1 and jobs(connection, opened.run_id) == {"succeeded": 1}


def test_account_error_gives_the_retry_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = db(populated(tmp_path, capsys, count=1))
    go(connection, ScriptedProvider({1: ProviderError(ErrorKind.AUTHENTICATION)}))
    assert connection.execute("SELECT round_attempts FROM llm_jobs").fetchone()[0] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_ctrl_c_exits_6_and_leaves_unknown_attempts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = populated(tmp_path, capsys, count=2)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "whatsapp_analyzer",
            "--json",
            "llm",
            "run",
            "-w",
            str(workspace),
            "--fake-scenario",
            "hang",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            probe = sqlite3.connect(workspace / DATABASE_FILE)
            found = probe.execute("SELECT count(*) FROM llm_attempts").fetchone()[0]
            probe.close()
        except sqlite3.Error:
            continue
        if found:
            break
    process.send_signal(signal.SIGINT)
    out, _ = process.communicate(timeout=30)
    assert process.returncode == ExitCode.RUN_PAUSED
    payload = json.loads(out)
    assert payload["data"]["status"] == "interrupted"
    connection = db(workspace)
    assert {r[0] for r in connection.execute("SELECT billing FROM llm_attempts")} == {"unknown"}


def test_twins_of_a_job_that_ran_out_of_retries_fail_with_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = twins(tmp_path, capsys)
    overloaded = ProviderError(ErrorKind.OVERLOADED)
    provider = ScriptedProvider({n: overloaded for n in range(1, 10)})
    opened, status = go(connection, provider, cfg(concurrency=2, max_attempts=2))
    assert provider.calls == 2 and status is RunStatus.COMPLETED_WITH_FAILURES
    assert jobs(connection, opened.run_id) == {"failed": 2}
    assert run_report(connection, opened.run_id)["run"]["partial"] is False  # type: ignore[index]


def test_resend_unknown_authorizes_only_the_requeued_jobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = twins(tmp_path, capsys)
    provider = ScriptedProvider({1: ProviderError(ErrorKind.TIMEOUT)}, per_request=True)
    go(connection, provider, cfg(concurrency=1))
    # A new run with --resend-unknown requeues nothing of its own, so it sends nothing.
    opened, _ = go(
        connection,
        provider,
        request=OpenRequest(force=True, resend_unknown=True),
        options=EngineOptions(reuse=False),
    )
    assert provider.calls == 1
    assert jobs(connection, opened.run_id) == {"awaiting_reconciliation": 2}


# M5-01: identical requests that are not adjacent in the queue --------------------------------

X_TEXT = "Identical words here?"


class XProvider(ScriptedProvider):
    """Scripted only for request X (jobs A and C); request Y (job B) always succeeds."""

    def __init__(self, script: dict[int, Action]) -> None:
        super().__init__()
        self.x_script = script
        self.x_calls = 0
        self.y_calls = 0

    async def complete(self, request: LLMRequest) -> LLMResult:
        if X_TEXT not in request.messages[0].content:
            self.y_calls += 1
            return await self.inner.complete(request)
        self.x_calls += 1
        self.script = (
            {self.calls + 1: self.x_script[self.x_calls]} if self.x_calls in self.x_script else {}
        )
        return await super().complete(request)


def abc(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> sqlite3.Connection:
    """Jobs in order A (request X), B (request Y), C (request X)."""
    workspace = new_workspace(tmp_path)
    rows = []
    for key, text in (("a", X_TEXT), ("b", "Something else entirely?"), ("c", X_TEXT)):
        rows += [
            row(key, "2026-01-05T10:00:00", "outbound", "Hello"),
            row(key, "2026-01-05T10:05:00", "inbound", text, sender="Same Person"),
        ]
    run_cli(["import", str(write_csv(tmp_path / "in.csv", rows)), "-w", str(workspace)], capsys)
    return db(workspace)


def by_key(connection: sqlite3.Connection, run_id: int) -> dict[str, tuple[str, str | None]]:
    return {
        r[0]: (r[1], r[2])
        for r in connection.execute(
            "SELECT c.conversation_key, j.status, j.error_kind FROM llm_jobs j "
            "JOIN conversation_versions v ON v.id = j.version_id "
            "JOIN conversations c ON c.id = v.conversation_id WHERE j.run_id = ?",
            (run_id,),
        )
    }


def result(finish: FinishReason, text: str = "not json") -> Callable[[LLMRequest], LLMResult]:
    return lambda request: LLMResult(text, finish, Usage(10, 0, 0, 5))


@pytest.mark.parametrize("concurrency", [1, 3])
@pytest.mark.parametrize(
    ("finish", "status"),
    [
        (FinishReason.STOP, "INVALID_RESPONSE"),
        (FinishReason.REFUSAL, "REFUSED"),
        (FinishReason.LENGTH, "INCOMPLETE_OUTPUT"),
        (FinishReason.CONTENT_FILTER, "CONTENT_FILTERED"),
    ],
)
def test_m5_01_billed_failure_is_shared_by_a_later_twin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    concurrency: int,
    finish: FinishReason,
    status: str,
) -> None:
    connection = abc(tmp_path, capsys)
    provider = XProvider({1: result(finish)})
    opened, _ = go(connection, provider, cfg(concurrency=concurrency))
    assert (provider.x_calls, provider.y_calls) == (1, 1)
    states = by_key(connection, opened.run_id)
    assert states["b"] == ("succeeded", None)
    assert {states["a"], states["c"]} == {("failed", None), ("failed", "DUPLICATE_OF_FAILED")}
    statuses = {
        r[0]
        for r in connection.execute(
            "SELECT execution_status FROM llm_jobs WHERE execution_status IS NOT NULL"
        )
    }
    assert status in statuses
    attempts = connection.execute(
        "SELECT count(*) FROM llm_attempts WHERE request_id = ("
        "SELECT j.request_id FROM llm_jobs j JOIN conversation_versions v ON v.id = j.version_id "
        "JOIN conversations c ON c.id = v.conversation_id WHERE c.conversation_key = 'a')"
    ).fetchone()[0]
    assert attempts == 1


@pytest.mark.parametrize("concurrency", [1, 3])
def test_m5_01_twin_respects_retry_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], concurrency: int
) -> None:
    connection = abc(tmp_path, capsys)
    clock = FakeClock()
    provider = XProvider({1: ProviderError(ErrorKind.RATE_LIMITED, retry_after_seconds=20)})
    sent_at: list[float] = []
    original = provider.complete

    async def timed(request: LLMRequest) -> LLMResult:
        if X_TEXT in request.messages[0].content:
            sent_at.append(clock.now())
        return await original(request)

    provider.complete = timed  # type: ignore[method-assign]
    opened, status = go(connection, provider, cfg(concurrency=concurrency), clock=clock)
    assert status is RunStatus.COMPLETED
    assert provider.x_calls == 2  # the first attempt and its retry, never the twin
    assert sent_at[1] - sent_at[0] >= 20
    states = by_key(connection, opened.run_id)
    assert {states["a"][0], states["c"][0]} == {"succeeded", "reused"}


def test_m5_01_exhausted_retries_are_shared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = abc(tmp_path, capsys)
    overloaded = ProviderError(ErrorKind.OVERLOADED)
    provider = XProvider({1: overloaded, 2: overloaded, 3: overloaded})
    opened, _ = go(connection, provider, cfg(concurrency=1, max_attempts=2))
    assert provider.x_calls == 2
    states = by_key(connection, opened.run_id)
    assert {states["a"], states["c"]} == {
        ("failed", "OVERLOADED"),
        ("failed", "DUPLICATE_OF_FAILED"),
    }


def test_m5_01_unknown_outcome_and_reconciliation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = abc(tmp_path, capsys)
    provider = XProvider({1: ProviderError(ErrorKind.TIMEOUT)})
    opened, _ = go(connection, provider, cfg(concurrency=1))
    assert provider.x_calls == 1
    states = by_key(connection, opened.run_id)
    assert {states["a"], states["c"]} == {
        ("awaiting_reconciliation", "TIMEOUT"),
        ("awaiting_reconciliation", "DUPLICATE_OF_UNKNOWN"),
    }
    # Explicitly authorized: X is sent once more, and the twin reuses it.
    _, status = go(
        connection,
        provider,
        cfg(concurrency=1),
        OpenRequest(run_id=opened.run_id, resend_unknown=True),
    )
    assert status is RunStatus.COMPLETED and provider.x_calls == 2
    states = by_key(connection, opened.run_id)
    assert {states["a"][0], states["c"][0]} == {"succeeded", "reused"}


def test_m5_01_interruption_after_the_first_twin_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = abc(tmp_path, capsys)
    provider = XProvider({1: result(FinishReason.STOP)})
    first: list[int] = []

    def crash_after_first_commit(job_id: int) -> None:
        first.append(job_id)
        if len(first) == 1:
            raise Crash

    with pytest.raises(Crash):
        go(
            connection,
            provider,
            cfg(concurrency=1),
            options=EngineOptions(hooks=Hooks(after_result_commit=crash_after_first_commit)),
        )
    assert provider.x_calls == 1
    go(connection, provider, cfg(concurrency=1))
    assert provider.x_calls == 1  # the twin reads the recorded failure from SQLite
    states = by_key(connection, 1)
    assert states["c"] == ("failed", "DUPLICATE_OF_FAILED") and states["b"][0] == "succeeded"


def test_m5_01_retry_failed_sends_the_request_once_for_all_twins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = abc(tmp_path, capsys)
    provider = XProvider({1: result(FinishReason.STOP)})
    opened, _ = go(connection, provider, cfg(concurrency=3))
    _, status = go(
        connection,
        provider,
        cfg(concurrency=3),
        OpenRequest(run_id=opened.run_id, retry_failed=True),
    )
    assert status is RunStatus.COMPLETED and provider.x_calls == 2
    states = by_key(connection, opened.run_id)
    assert {states["a"][0], states["c"][0]} == {"succeeded", "reused"}


def test_m5_01_sharing_never_duplicates_billing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = abc(tmp_path, capsys)
    provider = XProvider({})
    opened, _ = go(connection, provider, cfg(price=COMPLETE_PRICE, concurrency=1))
    assert provider.x_calls == 1
    attempts = connection.execute(
        "SELECT count(*), group_concat(cost) FROM llm_attempts"
    ).fetchone()
    assert attempts[0] == 2  # X once and Y once; the reused twin records nothing
    report = run_report(connection, opened.run_id)
    assert report["attempts"]["total"] == 2  # type: ignore[index]
    # --force --no-reuse: a new run calls again, but still only once per request.
    again, _ = go(
        connection,
        provider,
        cfg(price=COMPLETE_PRICE, concurrency=3),
        OpenRequest(force=True),
        options=EngineOptions(reuse=False),
    )
    assert provider.x_calls == 2
    states = by_key(connection, again.run_id)
    assert {states["a"][0], states["c"][0]} == {"succeeded", "reused"}


def test_m5_01_twins_are_settled_when_the_retry_owner_ends_without_sending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = abc(tmp_path, capsys)
    provider = XProvider({1: ProviderError(ErrorKind.RATE_LIMITED, retry_after_seconds=5)})
    original = engine_module.Engine._prepare
    calls: dict[int, int] = {}

    def prepare_fails_on_the_retry(self: object, job: object) -> object:
        job_id = job.id  # type: ignore[attr-defined]
        calls[job_id] = calls.get(job_id, 0) + 1
        if calls[job_id] == 2:  # the owner's retry can no longer be prepared
            self._copy_fate(job, JobStatus.FAILED, "CONTEXT_CHANGED")  # type: ignore[attr-defined]
            return None
        return original(self, job)  # type: ignore[arg-type]

    monkeypatch.setattr(engine_module.Engine, "_prepare", prepare_fails_on_the_retry)
    opened, status = go(connection, provider, cfg(concurrency=1), clock=FakeClock())
    assert status is RunStatus.COMPLETED_WITH_FAILURES and provider.x_calls == 1
    states = by_key(connection, opened.run_id)
    expected = {("failed", "CONTEXT_CHANGED"), ("failed", "DUPLICATE_OF_FAILED")}
    assert {states["a"], states["c"]} == expected
