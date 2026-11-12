"""`llm run` with real provider adapters over a mock transport: authorization, the M5 runner
(budgets, reuse, deduplication, reconciliation, resume) and secrets, end to end and offline.
"""

import dataclasses
import functools
import json
import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest

from helpers import new_workspace, row, run_cli, write_csv
from test_providers import KEY, OPENAI_USAGE, VALID, Transport, anthropic_body, openai_body
from whatsapp_analyzer.cli import consent
from whatsapp_analyzer.cli.commands import llm_run
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.llm import providers
from whatsapp_analyzer.store.db import connect
from whatsapp_analyzer.workspace import DATABASE_FILE

ANTHROPIC = """
[llm]
provider = "anthropic"
model = "claude-haiku-5-5"
[llm.connection]
api_key_env = "TEST_LLM_KEY"
"""


@pytest.fixture
def workspace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """Three synthetic conversations; b and c contain identical text (one request)."""
    root = new_workspace(tmp_path)
    rows = []
    for key, text in (("a", "Can we meet on Friday?"), ("b", "Same words?"), ("c", "Same words?")):
        rows += [
            row(key, "2026-01-05T10:00:00", "outbound", "Hello"),
            row(key, "2026-01-05T10:05:00", "inbound", text, sender="Contact"),
        ]
    code, _ = run_cli(
        ["import", str(write_csv(tmp_path / "in.csv", rows)), "-w", str(root)], capsys
    )
    assert code == 0
    return root


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", KEY)
    for name in ("OPENAI_LOG", "ANTHROPIC_LOG"):
        monkeypatch.delenv(name, raising=False)


def config(root: Path, text: str = ANTHROPIC, extra: str = "") -> Path:
    path = root.parent / "real.toml"
    path.write_text(text + extra, encoding="utf-8")
    return path


def mock(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx2.Request], Any]) -> Transport:
    """Route the real adapter of `llm run` through a mock transport."""
    transport = Transport(handler)
    monkeypatch.setattr(
        llm_run,
        "build_provider",
        functools.partial(providers.build_provider, transport=transport.client()),
    )
    return transport


def ok_anthropic(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=anthropic_body(), headers={"request-id": "req_1"})


def llm(
    root: Path, capsys: pytest.CaptureFixture[str], *args: str, cfg: Path | None = None
) -> tuple[int, dict[str, Any]]:
    argv = ["llm", "run", "-w", str(root), "--config", str(cfg or config(root)), *args]
    return run_cli(argv, capsys)


def db(root: Path) -> sqlite3.Connection:
    return connect(root / DATABASE_FILE)


def scalar(root: Path, sql: str, *params: object) -> Any:
    connection = db(root)
    try:
        return connection.execute(sql, params).fetchone()[0]
    finally:
        connection.close()


# Authorization -------------------------------------------------------------------------


def test_nothing_is_sent_without_authorization(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, ok_anthropic)
    code, payload = llm(workspace, capsys)
    assert code == ExitCode.AUTHORIZATION_REQUIRED
    assert int(ExitCode.AUTHORIZATION_REQUIRED) == 12  # public contract
    error = payload["error"]
    assert error["code"] == "AUTHORIZATION_REQUIRED"
    details = error["details"]
    assert details["authorize_with"] == ["--authorize-send", "anthropic"]
    assert details["destination"] == "anthropic" and details["loopback"] is False
    estimate = details["estimate"]
    assert (estimate["jobs"], estimate["requests"]) == (3, 2)  # b and c are one request
    assert estimate["max_requests"] == 6 and estimate["worst_case_cost"] is not None
    assert any("best-effort" in line for line in details["notice"])
    assert transport.requests == []
    assert scalar(workspace, "SELECT count(*) FROM llm_authorizations") == 0
    assert scalar(workspace, "SELECT count(*) FROM llm_attempts") == 0
    assert scalar(workspace, "SELECT count(*) FROM llm_runs") == 1  # planned, resumable
    # Left resumable and not "running", so it is never taken for a crash afterwards.
    status = scalar(workspace, "SELECT status || ':' || stop_reason FROM llm_runs")
    assert status == "interrupted:AUTHORIZATION_REQUIRED"
    assert details["run_id"] == 1 and details["notices"] == []
    _, report = run_cli(["llm", "status", "-w", str(workspace)], capsys)
    assert report["data"]["stale"] is False  # type: ignore[index]

    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic")
    assert code == 0 and payload["data"]["mode"] == "resumed"
    assert payload["data"]["run"]["stop_reason"] != "CRASH"
    assert payload["data"]["recovered_attempts"] == 0


def test_mismatched_authorization_is_refused_before_planning(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, ok_anthropic)
    code, payload = llm(workspace, capsys, "--authorize-send", "openai")
    assert code == 12 and payload["error"]["code"] == "AUTHORIZATION_MISMATCH"
    assert payload["error"]["details"] == {"configured_identity": "anthropic"}
    assert "openai" not in payload["error"]["message"]  # the given value is never echoed
    assert transport.requests == []
    assert scalar(workspace, "SELECT count(*) FROM llm_runs") == 0


def test_authorized_run_sends_each_distinct_request_once(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, ok_anthropic)
    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic")
    assert code == 0, payload
    data = payload["data"]
    assert data["destination"] == "anthropic" and data["authorization"] == "flag"
    assert data["jobs"]["succeeded"] + data["jobs"].get("reused", 0) == 3
    assert len(transport.requests) == 2  # the identical pair is deduplicated
    connection = db(workspace)
    try:
        auth = connection.execute(
            "SELECT method, provider, model, notice_version FROM llm_authorizations"
        ).fetchall()
        assert [tuple(r) for r in auth] == [("flag", "anthropic", "claude-haiku-5-5", 1)]
        providers_used = {r[0] for r in connection.execute("SELECT provider FROM llm_attempts")}
        assert providers_used == {"anthropic"}
        ids = {r[0] for r in connection.execute("SELECT provider_request_id FROM llm_attempts")}
        assert ids == {"req_1"}
    finally:
        connection.close()
    assert data["cost"]["known_total"] is not None  # built-in, sourced price

    # Running again: up to date, nothing to send, no new authorization needed.
    code, payload = llm(workspace, capsys)
    assert code == 0 and payload["data"]["mode"] == "up_to_date"
    assert len(transport.requests) == 2


def test_forced_run_reuses_stored_results_without_sending(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, ok_anthropic)
    assert llm(workspace, capsys, "--authorize-send", "anthropic")[0] == 0
    code, payload = llm(workspace, capsys, "--force", "--authorize-send", "anthropic")
    assert code == 0 and payload["data"]["jobs"]["reused"] == 3
    assert len(transport.requests) == 2  # nothing new was sent
    code, payload = llm(workspace, capsys, "--force", "--no-reuse", "--authorize-send", "anthropic")
    assert code == 0 and len(transport.requests) == 4  # explicitly asked to call again


def test_interactive_authorization(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, ok_anthropic)
    monkeypatch.setattr(consent, "_interactive", lambda state: True)
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "no thanks")
    code, payload = llm(workspace, capsys)
    assert code == 12 and payload["error"]["code"] == "AUTHORIZATION_DECLINED"
    assert transport.requests == []

    monkeypatch.setattr("typer.prompt", lambda *a, **k: "anthropic")
    code, payload = llm(workspace, capsys)
    assert code == 0 and payload["data"]["authorization"] == "interactive"
    assert payload["data"]["mode"] == "resumed"  # the run planned earlier
    assert len(transport.requests) == 2
    assert scalar(workspace, "SELECT method FROM llm_authorizations") == "interactive"


def test_resend_unknown_needs_a_new_authorization(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def read_timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("simulated", request=request)

    transport = mock(monkeypatch, read_timeout)
    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic")
    assert code == ExitCode.COMPLETED_WITH_FAILURES, payload
    assert payload["data"]["jobs"]["awaiting_reconciliation"] == 3
    assert len(transport.requests) == 2  # never resent automatically

    transport.handler = ok_anthropic
    code, payload = llm(workspace, capsys, "--resend-unknown")
    assert code == 12 and payload["error"]["code"] == "AUTHORIZATION_REQUIRED"
    assert len(transport.requests) == 2

    code, payload = llm(workspace, capsys, "--resend-unknown", "--authorize-send", "anthropic")
    assert code == 0, payload
    assert len(transport.requests) == 4
    assert scalar(workspace, "SELECT count(*) FROM llm_authorizations") == 2


def test_rejected_requests_are_retried_by_the_runner_only(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            body = {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}
            return httpx2.Response(429, json=body, headers={"retry-after": "0"})
        return ok_anthropic(request)

    mock(monkeypatch, handler)
    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic")
    assert code == 0, payload
    assert len(calls) == 3  # one rejected attempt retried by the runner, then two successes
    assert scalar(workspace, "SELECT count(*) FROM llm_attempts WHERE billing = 'rejected'") == 1


def test_account_error_pauses_the_run(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"type": "error", "error": {"type": "billing_error", "message": "no credit"}}
    transport = mock(monkeypatch, lambda r: httpx2.Response(402, json=body))
    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic")
    assert code == ExitCode.PROVIDER_ACCOUNT_ERROR, payload
    assert "INSUFFICIENT_CREDITS" in payload["data"]["run"]["stop_reason"]
    # requests already in flight (concurrency 4, two distinct requests) finish; none follow
    assert 1 <= len(transport.requests) <= 2
    code, payload = llm(workspace, capsys, "--concurrency", "1", "--authorize-send", "anthropic")
    assert code == ExitCode.PROVIDER_ACCOUNT_ERROR
    assert payload["data"]["mode"] == "resumed"


def test_budget_is_enforced_with_real_prices(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(monkeypatch, ok_anthropic)
    code, payload = llm(
        workspace, capsys, "--max-cost", "0.0000001", "--authorize-send", "anthropic"
    )
    assert code == ExitCode.BUDGET_REACHED, payload
    assert transport.requests == []


def test_response_without_usage_keeps_the_worst_case_charged(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    openai_cfg = config(
        workspace,
        '[llm]\nprovider = "openai"\nmodel = "gpt-6-luna"\n'
        '[llm.connection]\napi_key_env = "TEST_LLM_KEY"\n',
    )
    mock(monkeypatch, lambda r: httpx2.Response(200, json=openai_body(usage=None)))
    code, payload = llm(workspace, capsys, "--authorize-send", "openai", cfg=openai_cfg)
    assert code == 0, payload
    cost = payload["data"]["cost"]
    assert cost["known_total"] is None and cost["billed_attempts_without_cost"] == 2
    budget = payload["data"]["budget"]
    assert budget["committed_tokens"] > 0  # charged at the reservation, never as zero


def test_model_context_window_is_checked_before_sending(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "models.toml").write_text(
        'schema = 1\n[[models]]\nprovider = "anthropic"\nmodel = "tiny-model"\n'
        'context_window = 400\nsource = "test"\n',
        encoding="utf-8",
    )
    cfg = config(workspace, ANTHROPIC.replace("claude-haiku-5-5", "tiny-model"))
    transport = mock(monkeypatch, ok_anthropic)
    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic", cfg=cfg)
    assert payload["data"]["jobs"]["skipped"] == 3, payload
    assert transport.requests == []
    reasons = {r[0] for r in db(workspace).execute("SELECT DISTINCT skip_reason FROM llm_jobs")}
    assert reasons == {"MODEL_CONTEXT_EXCEEDED"}
    assert code in {ExitCode.OK, ExitCode.COMPLETED_WITH_FAILURES}


# Configuration and secrets -----------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "env", "code"),
    [
        ("", {"TEST_LLM_KEY": None}, "API_KEY_MISSING"),
        ("[llm.options]\nunknown = 1\n", {}, "OPTION_UNSUPPORTED"),
        ("", {"ANTHROPIC_LOG": "debug"}, "SDK_DEBUG_LOGGING"),
    ],
)
def test_configuration_errors_exit_4_without_sending(
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    extra: str,
    env: dict[str, str | None],
    code: str,
) -> None:
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, value)
    transport = mock(monkeypatch, ok_anthropic)
    exit_code, payload = llm(
        workspace, capsys, "--authorize-send", "anthropic", cfg=config(workspace, extra=extra)
    )
    assert exit_code == ExitCode.CONFIG_ERROR
    assert payload["error"]["code"] == code and payload["error"]["hint"]
    assert transport.requests == []
    assert scalar(workspace, "SELECT count(*) FROM llm_runs") == 0


def test_price_tier_ceiling_skips_prompts_that_would_be_under_costed(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "pricing.toml").write_text(
        'schema = 1\n[[prices]]\nprovider = "anthropic"\nmodel = "claude-haiku-5-5"\n'
        'currency = "USD"\nunit = "per_million_tokens"\ninput = "0.10"\ncache_read = "0.01"\n'
        'cache_write = "0.125"\noutput = "0.50"\nmax_input_tokens = 50\n'
        'effective_from = 2026-11-10\nretrieved_at = 2026-11-10\nsource = "test tier"\n',
        encoding="utf-8",
    )
    transport = mock(monkeypatch, ok_anthropic)
    _, payload = llm(workspace, capsys, "--authorize-send", "anthropic")
    assert payload["data"]["jobs"]["skipped"] == 3, payload
    assert transport.requests == []
    reasons = {r[0] for r in db(workspace).execute("SELECT DISTINCT skip_reason FROM llm_jobs")}
    assert reasons == {"PRICE_TIER_EXCEEDED"}


def test_provider_settings_are_refused_for_the_fake_provider(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, '[llm]\nprovider = "fake"\n[llm.options]\nefort = "low"\n')
    code, payload = llm(workspace, capsys, cfg=cfg)
    assert code == 4 and "llm.options" in payload["error"]["message"]


def test_temperature_for_a_model_that_rejects_it(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mock(monkeypatch, ok_anthropic)
    cfg = config(
        workspace, ANTHROPIC.replace("[llm.connection]", "temperature = 0.0\n[llm.connection]")
    )
    code, payload = llm(workspace, capsys, cfg=cfg)
    assert code == 4 and payload["error"]["code"] == "TEMPERATURE_UNSUPPORTED"


def test_key_never_reaches_storage_events_exports_or_output(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mock(monkeypatch, ok_anthropic)
    events = workspace.parent / "events.ndjson"
    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic", "--events", str(events))
    assert code == 0
    export = workspace.parent / "out.csv"
    run_cli(["llm", "export", "-w", str(workspace), "--out", str(export)], capsys)
    _, status = run_cli(["llm", "status", "-w", str(workspace)], capsys)
    for path in [*sorted(workspace.rglob("*")), events, export, Path(f"{export}.meta.json")]:
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
    assert KEY not in json.dumps(payload) and KEY not in json.dumps(status)
    assert "TEST_LLM_KEY" not in json.dumps(payload)  # not even the variable name is stored


def test_custom_endpoint_identity_and_loopback_notice(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "models.toml").write_text(
        'schema = 1\n[[models]]\nprovider = "openai-compatible"\nmodel = "local-model"\n'
        'structured_output = "json_object"\ncontext_window = 32000\nsource = "my server"\n',
        encoding="utf-8",
    )
    cfg = config(
        workspace,
        '[llm]\nprovider = "openai-compatible"\nmodel = "local-model"\n'
        '[llm.connection]\napi_key_env = "TEST_LLM_KEY"\nbase_url = "http://localhost:8000/v1"\n',
    )
    transport = mock(
        monkeypatch, lambda r: httpx2.Response(200, json=openai_body(usage=OPENAI_USAGE))
    )
    identity = "openai-compatible@http://localhost:8000/v1"
    code, payload = llm(workspace, capsys, cfg=cfg)
    assert code == 12
    details = payload["error"]["details"]
    assert details["destination"] == identity and details["loopback"] is True
    assert {"MODEL_NOT_IN_CATALOG", "PRICE_MISSING"} & set(details["notices"]) == {"PRICE_MISSING"}
    assert any("cannot verify" in line for line in details["notice"])

    code, payload = llm(workspace, capsys, "--authorize-send", identity, cfg=cfg)
    assert code == 0, payload
    assert {str(r.url) for r in transport.requests} == {"http://localhost:8000/v1/chat/completions"}
    assert "PRICE_MISSING" in payload["data"]["notices"]
    assert payload["data"]["cost"]["known_total"] is None


def test_results_are_not_reused_across_endpoints(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = mock(
        monkeypatch, lambda r: httpx2.Response(200, json=openai_body(usage=OPENAI_USAGE))
    )
    for port in (8000, 8001):
        cfg = config(
            workspace,
            '[llm]\nprovider = "openai-compatible"\nmodel = "m"\n[llm.connection]\n'
            f'api_key_env = "TEST_LLM_KEY"\nbase_url = "http://127.0.0.1:{port}/v1"\n',
        )
        identity = f"openai-compatible@http://127.0.0.1:{port}/v1"
        code, payload = llm(workspace, capsys, "--authorize-send", identity, cfg=cfg)
        assert code == 0, payload
    assert len(transport.requests) == 4  # two distinct requests per endpoint, none reused


# Offline guarantees and extensibility --------------------------------------------------


def test_offline_commands_never_build_a_real_provider(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a real provider was built")

    monkeypatch.setattr(llm_run, "build_provider", forbidden)
    monkeypatch.setattr(providers, "build_provider", forbidden)
    (workspace / "analyzer.toml").write_text(ANTHROPIC, encoding="utf-8")
    for args in (
        ["metrics", "run", "-w", str(workspace)],
        ["llm", "preview", "a", "-w", str(workspace), "--simulate"],
        ["llm", "prompts", "list"],
        ["status", "-w", str(workspace)],
    ):
        code, payload = run_cli(args, capsys)
        assert code in {0, 2}, (args, payload)
    # the fake provider never needs an authorization
    fake = config(workspace, '[llm]\nprovider = "fake"\n')
    code, result = llm(workspace, capsys, cfg=fake)
    assert code == 0 and result["data"]["authorization"] is None


def test_a_new_provider_is_registered_without_touching_the_runner(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The example adapter of docs/adding-a-provider.md, registered at test time."""
    from example_adapter import SPEC  # noqa: PLC0415

    monkeypatch.setitem(providers.REGISTRY, "example", SPEC)
    cfg = config(
        workspace,
        '[llm]\nprovider = "example"\nmodel = "echo-1"\n'
        '[llm.connection]\napi_key_env = "TEST_LLM_KEY"\n',
    )
    code, payload = llm(workspace, capsys, cfg=cfg)
    assert code == 12 and payload["error"]["details"]["destination"] == "example"
    code, payload = llm(workspace, capsys, "--authorize-send", "example", cfg=cfg)
    assert code == 0, payload
    assert payload["data"]["jobs"]["succeeded"] >= 2


def test_valid_constant_is_valid_json() -> None:
    assert json.loads(VALID)["level"] == "LOW"


# Codex audit fixes (M6-01 to M6-04) ------------------------------------------------------

GATEWAY = "openai-compatible@https://gateway.example:443/v1"
GATEWAY_CFG = (
    '[llm]\nprovider = "openai-compatible"\nmodel = "m"\n[llm.connection]\n'
    'api_key_env = "TEST_LLM_KEY"\nbase_url = "https://gateway.example/v1"\n'
)


def gateway(root: Path) -> Path:
    return config(root, GATEWAY_CFG)


def openai_ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=openai_body(usage=OPENAI_USAGE))


def attempts(root: Path) -> list[tuple[str, str | None]]:
    connection = db(root)
    try:
        rows = connection.execute("SELECT billing, error_kind FROM llm_attempts ORDER BY id")
        return [(r[0], r[1]) for r in rows]
    finally:
        connection.close()


def job_statuses(root: Path) -> list[str]:
    connection = db(root)
    try:
        return sorted(r[0] for r in connection.execute("SELECT status FROM llm_jobs"))
    finally:
        connection.close()


def test_gateway_400_after_processing_is_unknown_and_never_resent_automatically(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-call guardrail answers 400 after the model ran: billing is unknown."""
    body = {"error": {"type": "guardrail_block", "message": "post-call block"}}
    transport = mock(monkeypatch, lambda r: httpx2.Response(400, json=body))
    cfg = gateway(workspace)
    code, payload = llm(workspace, capsys, "--authorize-send", GATEWAY, cfg=cfg)
    assert code == ExitCode.COMPLETED_WITH_FAILURES, payload
    assert len(transport.requests) == 2  # two distinct requests, each sent once
    assert set(attempts(workspace)) == {("unknown", "ENDPOINT_ERROR")}
    assert payload["data"]["jobs"]["awaiting_reconciliation"] == 3
    reservations = payload["data"]["reservations"]
    assert reservations["unknown_billing_tokens"] > 0  # the reservation stays charged

    # Neither a resume nor --retry-failed sends it again ...
    for extra in ((), ("--retry-failed",)):
        code, payload = llm(workspace, capsys, *extra, "--authorize-send", GATEWAY, cfg=cfg)
        assert code == ExitCode.COMPLETED_WITH_FAILURES
    assert len(transport.requests) == 2
    # ... only an explicit, authorized --resend-unknown does.
    transport.handler = openai_ok
    code, payload = llm(workspace, capsys, "--resend-unknown", "--authorize-send", GATEWAY, cfg=cfg)
    assert code == 0, payload
    assert len(transport.requests) == 4


def test_gateway_429_is_not_retried_automatically(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway may apply its quota after processing: 429 then 200 must not mean two sends
    of the same request in one invocation."""
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) <= 2:
            body = {"error": {"type": "rate_limit_error", "message": "quota"}}
            return httpx2.Response(429, json=body, headers={"retry-after": "0"})
        return openai_ok(request)

    mock(monkeypatch, handler)
    code, payload = llm(workspace, capsys, "--authorize-send", GATEWAY, cfg=gateway(workspace))
    assert code == ExitCode.COMPLETED_WITH_FAILURES, payload
    assert len(calls) == 2  # one per distinct request, no automatic retry
    assert set(attempts(workspace)) == {("unknown", "ENDPOINT_ERROR")}


def test_gateway_transport_rules_are_unchanged(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never connected: provably not sent, safe to retry. Read timeout: unknown."""
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append("x")
        if len(calls) == 1:
            raise httpx2.ConnectError("refused", request=request)
        if len(calls) == 2:
            raise httpx2.ReadTimeout("slow", request=request)
        return openai_ok(request)

    monkeypatch.setattr(
        llm_run,
        "build_provider",
        functools.partial(providers.build_provider, transport=Transport(handler).client()),
    )
    code, _ = llm(
        workspace, capsys, "--concurrency", "1", "--authorize-send", GATEWAY, cfg=gateway(workspace)
    )
    assert code == ExitCode.COMPLETED_WITH_FAILURES
    billing = [b for b, _ in attempts(workspace)]
    assert billing[0] == "not_sent" and "unknown" in billing and "billed" in billing


def test_official_rate_limit_is_still_a_proven_refusal(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            body = {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}
            return httpx2.Response(429, json=body, headers={"retry-after": "0"})
        return ok_anthropic(request)

    mock(monkeypatch, handler)
    code, _ = llm(workspace, capsys, "--concurrency", "1", "--authorize-send", "anthropic")
    assert code == 0
    assert attempts(workspace)[0] == ("rejected", "RATE_LIMITED")  # documented: not processed


@pytest.mark.parametrize("flag", ["--resend-unknown", "--retry-failed"])
def test_refused_authorization_leaves_no_resend_pending(
    flag: str,
    workspace: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if flag == "--resend-unknown":

        def first(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("slow", request=request)

        expected = ["awaiting_reconciliation"] * 3
    else:
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}

        def first(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(400, json=body)

        expected = ["failed"] * 3
    transport = mock(monkeypatch, first)
    assert llm(workspace, capsys, "--authorize-send", "anthropic")[0] == 8
    sent = len(transport.requests)
    assert job_statuses(workspace) == expected

    transport.handler = ok_anthropic
    code, payload = llm(workspace, capsys, flag)  # no authorization
    assert code == 12 and payload["error"]["code"] == "AUTHORIZATION_REQUIRED"
    assert payload["error"]["details"]["estimate"]["jobs"] == 3  # what the re-send would send
    assert job_statuses(workspace) == expected  # nothing was requeued
    assert len(transport.requests) == sent

    # Authorizing the destination alone does not re-send: the explicit flag is needed again.
    code, payload = llm(workspace, capsys, "--authorize-send", "anthropic")
    assert code == 8 and len(transport.requests) == sent
    assert job_statuses(workspace) == expected

    code, payload = llm(workspace, capsys, flag, "--authorize-send", "anthropic")
    assert code == 0, payload
    assert len(transport.requests) == sent + 2
    assert payload["data"]["requeued"][flag.removeprefix("--").replace("-", "_")] == 3


def test_invalid_content_fails_only_its_own_job(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = iter([12, VALID])  # an integer "content", then a valid answer

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=openai_body(text=next(replies), usage=OPENAI_USAGE))  # type: ignore[arg-type]

    mock(monkeypatch, handler)
    cfg = config(
        workspace,
        '[llm]\nprovider = "openai"\nmodel = "gpt-6-luna"\n'
        '[llm.connection]\napi_key_env = "TEST_LLM_KEY"\n',
    )
    code, payload = llm(
        workspace, capsys, "--concurrency", "1", "--authorize-send", "openai", cfg=cfg
    )
    assert code == ExitCode.COMPLETED_WITH_FAILURES, payload  # finished, not INTERNAL_ERROR
    jobs = payload["data"]["jobs"]
    assert jobs["awaiting_reconciliation"] == 1 and jobs["succeeded"] + jobs["reused"] == 2
    assert ("unknown", "MALFORMED_PROVIDER_RESPONSE") in attempts(workspace)


def test_resumed_run_snapshot_gains_the_long_context_tier(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run planned with a snapshot that had no tier (as before the fix) is costed with the
    long rates when it resumes and a request exceeds 272K input tokens."""
    usage = {"prompt_tokens": 300000, "completion_tokens": 1000, "total_tokens": 301000}
    usage["prompt_tokens_details"] = {"cached_tokens": 0, "cache_write_tokens": 0}  # type: ignore[assignment]
    mock(monkeypatch, lambda r: httpx2.Response(200, json=openai_body(usage=usage)))
    cfg = config(
        workspace,
        '[llm]\nprovider = "openai"\nmodel = "gpt-6-luna"\n'
        '[llm.connection]\napi_key_env = "TEST_LLM_KEY"\n',
    )
    assert llm(workspace, capsys, cfg=cfg)[0] == 12  # planned, not sent
    connection = db(workspace)
    snapshot = json.loads(connection.execute("SELECT price_json FROM llm_runs").fetchone()[0])
    for name in ("max_input_tokens", "long_input", "long_cache_read", "long_cache_write"):
        snapshot[name] = None
    snapshot["long_output"] = None
    with connection:
        connection.execute("UPDATE llm_runs SET price_json = ?", (json.dumps(snapshot),))
    connection.close()

    code, payload = llm(workspace, capsys, "--authorize-send", "openai", cfg=cfg)
    assert code == 0, payload
    stored = json.loads(scalar(workspace, "SELECT price_json FROM llm_runs"))
    assert stored["max_input_tokens"] == 272000 and stored["long_output"] == "0.75"
    costs = {r[0] for r in db(workspace).execute("SELECT cost FROM llm_attempts")}
    long_cost = (Decimal(300000) * Decimal("0.20") + Decimal(1000) * Decimal("0.75")) / 10**6
    assert {Decimal(c) for c in costs} == {long_cost}


def test_snapshot_upgrade_never_lowers_a_rate() -> None:
    from whatsapp_analyzer.llm.pricing import builtin_prices  # noqa: PLC0415
    from whatsapp_analyzer.runner.service import with_known_tier  # noqa: PLC0415

    current = builtin_prices().lookup("openai", "gpt-6-luna", __import__("datetime").date.today())
    assert current is not None
    expensive = dataclasses.replace(
        current,
        input=Decimal(5),
        output=Decimal(9),
        max_input_tokens=None,
        long_input=None,
        long_cache_read=None,
        long_cache_write=None,
        long_output=None,
    )
    upgraded = with_known_tier(expensive, current)
    assert upgraded is not None and upgraded.max_input_tokens == 272000
    assert upgraded.long_input == Decimal(5) and upgraded.long_output == Decimal(9)
    assert with_known_tier(current, current) is current  # already tiered: unchanged
