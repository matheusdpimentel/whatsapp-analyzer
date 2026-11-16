"""Milestone 7: independent review and consolidation (offline: fake and mock providers)."""

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from helpers import new_workspace, row, run_cli, write_csv
from test_provider_runs import mock, ok_anthropic
from test_providers import KEY
from whatsapp_analyzer.config import ConsolidationPolicy as P
from whatsapp_analyzer.exit_codes import ExitCode
from whatsapp_analyzer.runner.review import Outcome, decide
from whatsapp_analyzer.store.db import connect
from whatsapp_analyzer.workspace import DATABASE_FILE

PRIMARY = '[llm]\nprovider = "fake"\nmodel = "fake-classifier"\n'
FAKE_DUAL = PRIMARY + '[llm.review]\nmode = "dual"\nprovider = "fake"\nmodel = "fake-reviewer"\n'
ANTHROPIC_DUAL = PRIMARY + (
    '[llm.review]\nmode = "dual"\nprovider = "anthropic"\nmodel = "claude-haiku-5-5"\n'
    '[llm.review.connection]\napi_key_env = "TEST_LLM_KEY"\n'
)


@pytest.fixture
def workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("TEST_LLM_KEY", KEY)
    root = new_workspace(tmp_path)
    rows = []
    for key, text in (("a", "Can we meet on Friday?"), ("b", "ok"), ("c", "Thanks, see you")):
        rows += [
            row(key, "2026-01-05T10:00:00", "outbound", "Hello"),
            row(key, "2026-01-05T10:05:00", "inbound", text, sender="Contact"),
        ]
    assert (
        run_cli(["import", str(write_csv(tmp_path / "in.csv", rows)), "-w", str(root)], capsys)[0]
        == 0
    )
    return root


def config(root: Path, text: str) -> Path:
    path = root.parent / "cfg.toml"
    path.write_text(text, encoding="utf-8")
    return path


def cli(
    root: Path, capsys: pytest.CaptureFixture[str], *args: str, cfg: Path
) -> tuple[int, dict[str, Any]]:
    head, tail = list(args[:2]), list(args[2:])
    return run_cli([*head, "-w", str(root), "--config", str(cfg), *tail], capsys)


def jobs(root: Path, run_id: int) -> list[sqlite3.Row]:
    connection = connect(root / DATABASE_FILE)
    try:
        return connection.execute(
            "SELECT * FROM llm_jobs WHERE run_id = ? ORDER BY version_id", (run_id,)
        ).fetchall()
    finally:
        connection.close()


def primary(root: Path, capsys: pytest.CaptureFixture[str], cfg: Path, *extra: str) -> int:
    code, payload = cli(root, capsys, "llm", "run", *extra, cfg=cfg)
    assert code == 0, payload
    return int(payload["data"]["run_id"])


def consolidated(
    root: Path, capsys: pytest.CaptureFixture[str], cfg: Path, *extra: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    out = root.parent / "consolidated.csv"
    code, payload = cli(
        root, capsys, "llm", "consolidate", "--out", str(out), "--overwrite", *extra, cfg=cfg
    )
    assert code == 0, payload
    with out.open(encoding="utf-8") as handle:
        return payload["data"], list(csv.DictReader(handle))


# Decision table -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("primary_label", "reviewer_label", "reviewed", "policy", "expected"),
    [
        (
            "HIGH",
            None,
            False,
            P.HUMAN_REVIEW,
            Outcome("not_reviewed", "primary_only", "HIGH", "primary"),
        ),
        (None, None, False, P.HUMAN_REVIEW, Outcome("not_reviewed", "no_result", None, None)),
        ("LOW", "LOW", True, P.REQUIRE_AGREEMENT, Outcome("agree", "agreed", "LOW", "both")),
        ("LOW", "HIGH", True, P.REQUIRE_AGREEMENT, Outcome("disagree", "unresolved", None, None)),
        (
            "LOW",
            "HIGH",
            True,
            P.PREFER_PRIMARY,
            Outcome("disagree", "preferred_primary", "LOW", "primary"),
        ),
        (
            "LOW",
            "HIGH",
            True,
            P.PREFER_REVIEWER,
            Outcome("disagree", "preferred_reviewer", "HIGH", "reviewer"),
        ),
        (
            "LOW",
            "HIGH",
            True,
            P.HUMAN_REVIEW,
            Outcome("disagree", "needs_human_review", None, None),
        ),
        (
            None,
            "HIGH",
            True,
            P.PREFER_PRIMARY,
            Outcome("incomplete", "single_result", "HIGH", "reviewer"),
        ),
        (
            "LOW",
            None,
            True,
            P.PREFER_REVIEWER,
            Outcome("incomplete", "single_result", "LOW", "primary"),
        ),
        ("LOW", None, True, P.REQUIRE_AGREEMENT, Outcome("incomplete", "unresolved", None, None)),
        (
            "LOW",
            None,
            True,
            P.HUMAN_REVIEW,
            Outcome("incomplete", "needs_human_review", None, None),
        ),
        (None, None, True, P.PREFER_PRIMARY, Outcome("incomplete", "no_result", None, None)),
    ],
)
def test_consolidation_decisions_are_categorical(
    primary_label: str | None,
    reviewer_label: str | None,
    reviewed: bool,
    policy: P,
    expected: Outcome,
) -> None:
    assert decide(primary_label, reviewer_label, reviewed, policy) == expected


# Dual mode ------------------------------------------------------------------------------


def test_dual_review_is_independent_and_preserves_the_primary_run(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    primary_id = primary(workspace, capsys, cfg)
    before = [tuple(r) for r in jobs(workspace, primary_id)]
    code, payload = cli(workspace, capsys, "llm", "review", "--fake-scenario", "review", cfg=cfg)
    assert code == 0, payload
    data = payload["data"]
    assert data["review"]["mode"] == "dual" and data["review"]["selected"] == 3
    review_id = data["run_id"]
    assert review_id != primary_id
    after = [tuple(r) for r in jobs(workspace, primary_id)]
    assert after == before  # the primary results are never touched
    # Same prepared conversation and template, a different request: the reviewer never sees
    # the primary classification (the rendered prompt differs only by the model id).
    for mine, theirs in zip(jobs(workspace, primary_id), jobs(workspace, review_id), strict=True):
        assert mine["version_id"] == theirs["version_id"]
        assert mine["context_hash"] == theirs["context_hash"]
        assert mine["request_id"] != theirs["request_id"]
        assert theirs["label"] == "REVIEW_REQUIRED"
    connection = connect(workspace / DATABASE_FILE)
    templates = connection.execute(
        "SELECT DISTINCT prompt_id, prompt_version, template_hash FROM llm_runs"
    ).fetchall()
    connection.close()
    assert len(templates) == 1

    summary, rows = consolidated(workspace, capsys, cfg)
    assert summary["review_run"]["id"] == review_id and summary["rows"] == 3
    assert summary["by_consolidation"] == {"needs_human_review": 3}  # default policy
    assert {r["agreement"] for r in rows} == {"disagree"}
    assert all(r["needs_human_review"] == "true" and r["consolidated_label"] == "" for r in rows)
    assert all(r["primary_label"] and r["reviewer_label"] == "REVIEW_REQUIRED" for r in rows)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("require_agreement", {"unresolved": 3}),
        ("prefer_primary", {"preferred_primary": 3}),
        ("prefer_reviewer", {"preferred_reviewer": 3}),
        ("human_review", {"needs_human_review": 3}),
    ],
)
def test_policies(
    policy: str, expected: dict[str, int], workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    primary(workspace, capsys, cfg)
    assert cli(workspace, capsys, "llm", "review", "--fake-scenario", "review", cfg=cfg)[0] == 0
    summary, rows = consolidated(workspace, capsys, cfg, "--policy", policy)
    assert summary["by_consolidation"] == expected and summary["policy"] == policy
    if policy == "prefer_primary":
        assert all(r["consolidated_label"] == r["primary_label"] for r in rows)
    if policy == "prefer_reviewer":  # REVIEW_REQUIRED still goes to a person
        assert all(
            r["consolidated_label"] == "REVIEW_REQUIRED" and r["needs_human_review"] == "true"
            for r in rows
        )


def test_agreement_without_review_required(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    primary(workspace, capsys, cfg)
    assert cli(workspace, capsys, "llm", "review", cfg=cfg)[0] == 0  # same fake behavior
    summary, rows = consolidated(workspace, capsys, cfg, "--policy", "require_agreement")
    assert summary["by_consolidation"] == {"agreed": 3} and summary["needs_human_review"] == 0
    assert all(r["consolidated_from"] == "both" for r in rows)


# Selective mode -------------------------------------------------------------------------


def test_selective_review_uses_explicit_criteria(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = PRIMARY + (
        '[llm.review]\nmode = "selective"\ncriteria = ["review_required"]\n'
        'provider = "fake"\nmodel = "fake-reviewer"\n'
    )
    cfg = config(workspace, text)
    primary(workspace, capsys, cfg)  # the fake answers are never REVIEW_REQUIRED here
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 0 and payload["data"] == {"primary_run": 1, "selected": 0, "selection": {}}
    connection = connect(workspace / DATABASE_FILE)
    assert connection.execute("SELECT count(*) FROM llm_runs").fetchone()[0] == 1  # nothing planned
    connection.close()

    code, payload = cli(workspace, capsys, "llm", "review", "--conversation", "b", cfg=cfg)
    assert code == 0, payload
    review = payload["data"]["review"]
    assert review["selected"] == 1 and review["selection"] == {"conversation": 1}
    summary, rows = consolidated(workspace, capsys, cfg)
    assert summary["reviewed"] == 1 and summary["mode"] == "selective"
    by_key = {r["conversation_key"]: r for r in rows}
    assert by_key["b"]["reviewed"] == "true" and by_key["b"]["consolidation"] == "agreed"
    assert by_key["a"]["consolidation"] == by_key["c"]["consolidation"] == "primary_only"


def test_selective_review_of_unlabeled_and_review_required(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = PRIMARY + (
        '[llm.review]\nmode = "selective"\ncriteria = ["review_required", "unlabeled"]\n'
        'provider = "fake"\nmodel = "fake-reviewer"\n'
    )
    cfg = config(workspace, text)
    run_id = primary(workspace, capsys, cfg, "--fake-scenario", "review")  # REVIEW_REQUIRED
    # a fake scenario is a provider identity of its own: name the primary run explicitly
    code, payload = cli(workspace, capsys, "llm", "review", "--primary-run", str(run_id), cfg=cfg)
    assert code == 0, payload
    assert payload["data"]["review"]["selection"] == {"review_required": 3}


def test_metric_disagreement_compares_with_deterministic_levels(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = PRIMARY + (
        '[llm.review]\nmode = "selective"\ncriteria = ["metric_disagreement"]\n'
        'provider = "fake"\nmodel = "fake-reviewer"\n'
    )
    cfg = config(workspace, text)
    assert run_cli(["metrics", "run", "-w", str(workspace)], capsys)[0] == 0
    run_id = primary(workspace, capsys, cfg)
    connection = connect(workspace / DATABASE_FILE)
    expected = connection.execute(
        "SELECT count(*) FROM llm_jobs j JOIN conversation_scores s ON s.version_id = j.version_id "
        "WHERE j.run_id = ? AND j.label IN ('LOW','MEDIUM','HIGH') "
        "AND s.level IN ('LOW','MEDIUM','HIGH') AND j.label != s.level",
        (run_id,),
    ).fetchone()[0]
    connection.close()
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 0, payload
    selected = (
        payload["data"]["selected"] if expected == 0 else payload["data"]["review"]["selected"]
    )
    assert selected == expected
    if expected:
        _, rows = consolidated(workspace, capsys, cfg)
        assert all(r["metric_level"] for r in rows)


# Real reviewer: authorization, budget, resume -------------------------------------------


def test_real_reviewer_needs_its_own_authorization_and_budget(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(workspace, ANTHROPIC_DUAL)
    primary(workspace, capsys, cfg)  # the fake primary needs no authorization
    transport = mock(monkeypatch, ok_anthropic)

    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 12 and payload["error"]["details"]["destination"] == "anthropic"
    code, payload = cli(workspace, capsys, "llm", "review", "--authorize-send", "fake", cfg=cfg)
    assert code == 12 and payload["error"]["code"] == "AUTHORIZATION_MISMATCH"
    assert transport.requests == []

    code, payload = cli(
        workspace,
        capsys,
        "llm",
        "review",
        "--max-cost",
        "0.0000001",
        "--authorize-send",
        "anthropic",
        cfg=cfg,
    )
    assert code == ExitCode.BUDGET_REACHED and transport.requests == []
    review_id = payload["data"]["run_id"]

    # resuming keeps the stored budget unless it is raised explicitly (M5)
    code, payload = cli(
        workspace,
        capsys,
        "llm",
        "review",
        "--max-cost",
        "10",
        "--authorize-send",
        "anthropic",
        cfg=cfg,
    )
    assert code == 0, payload
    assert payload["data"]["run_id"] == review_id and payload["data"]["mode"] == "resumed"
    assert len(transport.requests) == 3
    connection = connect(workspace / DATABASE_FILE)
    links = connection.execute(
        "SELECT primary_run_id, review_run_id, mode FROM llm_reviews"
    ).fetchall()
    auth = connection.execute("SELECT provider FROM llm_authorizations").fetchall()
    connection.close()
    assert [tuple(r) for r in links] == [(1, review_id, "dual")]  # one link, resumed in place
    assert [r[0] for r in auth] == ["anthropic", "anthropic"]


def test_reviewer_limits_are_its_own(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        ANTHROPIC_DUAL.replace("[llm]\n", "[llm]\n")
        + '[llm.review.limits]\nmax_cost = "0.0000001"\n'
    )
    cfg = config(workspace, text)
    primary(workspace, capsys, cfg)
    transport = mock(monkeypatch, ok_anthropic)
    code, payload = cli(
        workspace, capsys, "llm", "review", "--authorize-send", "anthropic", cfg=cfg
    )
    assert code == ExitCode.BUDGET_REACHED and transport.requests == []
    from decimal import Decimal  # noqa: PLC0415

    assert Decimal(payload["data"]["run"]["limits"]["max_cost"]) == Decimal("0.0000001")


# Guard rails ----------------------------------------------------------------------------


def test_guard_rails(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    single = config(workspace, PRIMARY)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=single)
    assert code == 4 and payload["error"]["code"] == "REVIEW_DISABLED"

    cfg = config(workspace, FAKE_DUAL)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 2 and payload["error"]["code"] == "PRIMARY_RUN_NOT_FOUND"

    same = config(
        workspace,
        PRIMARY + '[llm.review]\nmode = "dual"\nprovider = "fake"\nmodel = "fake-classifier"\n',
    )
    primary(workspace, capsys, same)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=same)
    assert code == 4 and payload["error"]["code"] == "REVIEWER_SAME_AS_PRIMARY"

    code, payload = cli(
        workspace, capsys, "llm", "consolidate", "--out", str(workspace.parent / "x.csv"), cfg=cfg
    )
    assert code == 2 and payload["error"]["code"] == "REVIEW_NOT_FOUND"
    code, payload = cli(
        workspace,
        capsys,
        "llm",
        "consolidate",
        "--out",
        str(workspace.parent / "x.csv"),
        "--policy",
        "average",
        cfg=cfg,
    )
    assert code == 2 and payload["error"]["code"] == "INVALID_OPTION"


def test_unfinished_primary_run_is_not_reviewed(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    code, _ = cli(workspace, capsys, "llm", "run", "--max-tokens", "1", cfg=cfg)
    assert code == ExitCode.BUDGET_REACHED
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 2 and payload["error"]["code"] == "PRIMARY_RUN_UNFINISHED"


def test_review_keeps_the_versions_of_the_primary_run(
    workspace: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    primary_id = primary(workspace, capsys, cfg)
    newer = [  # the earlier messages plus new ones: a new version of "a"
        row("a", "2026-01-05T10:00:00", "outbound", "Hello"),
        row("a", "2026-01-05T10:05:00", "inbound", "Can we meet on Friday?", sender="Contact"),
        row("a", "2026-01-06T10:00:00", "outbound", "New day"),
    ]
    code, _ = run_cli(
        ["import", str(write_csv(tmp_path / "newer.csv", newer)), "-w", str(workspace)], capsys
    )
    assert code == 0
    connection = connect(workspace / DATABASE_FILE)
    current = connection.execute(
        "SELECT current_version_id FROM conversations WHERE conversation_key = 'a'"
    ).fetchone()[0]
    connection.close()
    primary_versions = [r["version_id"] for r in jobs(workspace, primary_id)]
    assert current not in primary_versions  # "a" really has a newer version now
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 0, payload
    assert [r["version_id"] for r in jobs(workspace, payload["data"]["run_id"])] == primary_versions


def test_review_configuration_errors() -> None:
    import tomllib  # noqa: PLC0415

    from whatsapp_analyzer.config import ConfigError, parse_settings  # noqa: PLC0415

    cases = {
        '[llm.review]\nmode = "dual"\nmodel = "m"\n': ("missing_key", "llm.review.provider"),
        '[llm.review]\nmode = "triple"\n': ("invalid_choice", "llm.review.mode"),
        '[llm.review]\npolicy = "average"\n': ("invalid_choice", "llm.review.policy"),
        '[llm.review]\ncriteria = ["confidence"]\n': ("invalid_choice", "llm.review.criteria"),
        "[llm.review]\ncriteria = []\n": ("wrong_type", "llm.review.criteria"),
        "[llm.review.context]\nmax_messages = 3\n": ("unknown_key", "llm.review.context"),
        "[llm.review]\nweights = {a = 1}\n": ("unknown_key", "llm.review.weights"),
    }
    for text, (kind, key) in cases.items():
        with pytest.raises(ConfigError) as info:
            parse_settings(tomllib.loads(text))
        assert (info.value.kind, info.value.details["key"]) == (kind, key), text
    settings = parse_settings(
        tomllib.loads(
            '[llm.limits]\nmax_cost = "2"\n[llm.review]\nmode = "selective"\n'
            'provider = "fake"\nmodel = "r"\n'
        )
    )
    reviewer = settings.llm.review.reviewer
    assert reviewer is not None and reviewer.limits.max_cost is not None  # inherited unless set
    assert reviewer.context == settings.llm.context  # always the primary context


def test_consolidation_export_has_no_content(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    primary(workspace, capsys, cfg)
    assert cli(workspace, capsys, "llm", "review", cfg=cfg)[0] == 0
    consolidated(workspace, capsys, cfg)
    out = workspace.parent / "consolidated.csv"
    meta = json.loads(Path(f"{out}.meta.json").read_text(encoding="utf-8"))
    assert meta["content_included"] is False and "never averages" in meta["note"]
    body = out.read_text(encoding="utf-8")
    assert "Can we meet" not in body and "Thanks" not in body


def test_reviewer_inherits_each_primary_limit_it_does_not_set() -> None:
    import tomllib  # noqa: PLC0415

    from whatsapp_analyzer.config import parse_settings  # noqa: PLC0415

    settings = parse_settings(
        tomllib.loads(
            '[llm.limits]\nmax_cost = "1.00"\nmax_tokens = 1000\n'
            '[llm.review]\nmode = "dual"\nprovider = "fake"\nmodel = "r"\n'
            "[llm.review.limits]\nrequests_per_minute = 5\n"
        )
    )
    reviewer = settings.llm.review.reviewer
    assert reviewer is not None
    limits = reviewer.limits
    assert (limits.requests_per_minute, limits.max_tokens, str(limits.max_cost)) == (
        5,
        1000,
        "1.00",
    )


def test_refused_review_is_not_linked(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(workspace, ANTHROPIC_DUAL)
    primary(workspace, capsys, cfg)
    mock(monkeypatch, ok_anthropic)
    assert cli(workspace, capsys, "llm", "review", cfg=cfg)[0] == 12
    connection = connect(workspace / DATABASE_FILE)
    assert connection.execute("SELECT count(*) FROM llm_reviews").fetchone()[0] == 0
    connection.close()
    code, payload = cli(
        workspace, capsys, "llm", "consolidate", "--out", str(workspace.parent / "x.csv"), cfg=cfg
    )
    assert code == 2 and payload["error"]["code"] == "REVIEW_NOT_FOUND"


def test_conversation_keys_are_refused_in_dual_mode(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    primary(workspace, capsys, cfg)
    code, payload = cli(workspace, capsys, "llm", "review", "--conversation", "a", cfg=cfg)
    assert code == 2 and payload["error"]["code"] == "INCOMPATIBLE_OPTIONS"


# Codex audit of M7: independence, context and selection stability -------------------------

SCORE_LOW = "[scoring.thresholds]\nhigh = 1.0\nmedium = 0.99\n"
SCORE_MEDIUM = "[scoring.thresholds]\nhigh = 1.0\nmedium = 0.01\n"


def links(root: Path) -> list[tuple[Any, ...]]:
    connection = connect(root / DATABASE_FILE)
    try:
        return [
            tuple(r)
            for r in connection.execute("SELECT primary_run_id, review_run_id FROM llm_reviews")
        ]
    finally:
        connection.close()


def runs(root: Path) -> int:
    connection = connect(root / DATABASE_FILE)
    try:
        return int(connection.execute("SELECT count(*) FROM llm_runs").fetchone()[0])
    finally:
        connection.close()


@pytest.mark.parametrize(
    "reviewer",
    [
        'provider = "fake"\nmodel = "fake-classifier"\nmax_output_tokens = 999\n',  # other settings
        'provider = "fake"\nmodel = "fake-classifier"\nprompt = "engagement-classification"\n',
    ],
)
def test_same_provider_and_model_is_never_an_independent_review(
    reviewer: str, workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, PRIMARY + '[llm.review]\nmode = "dual"\n' + reviewer)
    primary(workspace, capsys, cfg)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 4 and payload["error"]["code"] == "REVIEWER_SAME_AS_PRIMARY"
    assert runs(workspace) == 1 and links(workspace) == []


def test_changed_ignored_texts_cannot_reuse_the_primary_results(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The audit's false consensus: same model, ignored_texts changed to dodge the run key."""
    same = PRIMARY + '[llm.review]\nmode = "dual"\nprovider = "fake"\nmodel = "fake-classifier"\n'
    primary(workspace, capsys, config(workspace, same))
    changed = config(workspace, '[metrics]\nignored_texts = ["synthetic line"]\n' + same)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=changed)
    assert code == 4 and payload["error"]["code"] == "REVIEWER_SAME_AS_PRIMARY"
    # with another model the changed ignored_texts are a different context: refused too
    other = config(workspace, '[metrics]\nignored_texts = ["synthetic line"]\n' + FAKE_DUAL)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=other)
    assert code == 4 and payload["error"]["code"] == "REVIEW_CONTEXT_MISMATCH"
    assert runs(workspace) == 1


def test_reviewer_must_see_the_primary_context(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = primary(
        workspace,
        capsys,
        config(
            workspace, PRIMARY + "[llm.context]\nmax_messages = 1\n" + FAKE_DUAL[len(PRIMARY) :]
        ),
    )
    wider = config(
        workspace, PRIMARY + "[llm.context]\nmax_messages = 200\n" + FAKE_DUAL[len(PRIMARY) :]
    )
    code, payload = cli(workspace, capsys, "llm", "review", "--primary-run", str(run_id), cfg=wider)
    assert code == 4 and payload["error"]["code"] == "REVIEW_CONTEXT_MISMATCH"
    assert payload["error"]["details"] == {"primary_run": run_id}
    assert runs(workspace) == 1 and links(workspace) == []
    same = config(
        workspace, PRIMARY + "[llm.context]\nmax_messages = 1\n" + FAKE_DUAL[len(PRIMARY) :]
    )
    code, payload = cli(workspace, capsys, "llm", "review", cfg=same)
    assert code == 0, payload
    for mine, theirs in zip(
        jobs(workspace, run_id), jobs(workspace, payload["data"]["run_id"]), strict=True
    ):
        assert mine["context_hash"] == theirs["context_hash"]


def test_consolidation_never_counts_a_copied_result_as_agreement(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Defense in depth: a link to a run that reused the primary results (made here by hand)
    is reported as not independent, never as `agreed` / `both`."""
    cfg = config(workspace, PRIMARY)
    first = primary(workspace, capsys, cfg)
    code, payload = cli(workspace, capsys, "llm", "run", "--force", cfg=cfg)
    assert code == 0 and payload["data"]["jobs"]["reused"] == 3
    second = payload["data"]["run_id"]
    connection = connect(workspace / DATABASE_FILE)
    with connection:
        connection.execute(
            "INSERT INTO llm_reviews (primary_run_id, review_run_id, mode, selection_key, "
            "criteria_json, policy, selected, created_at, used_seq) "
            "VALUES (?, ?, 'dual', 'manual', '{}', 'require_agreement', 3, 'x', 1)",
            (first, second),
        )
    connection.close()
    summary, rows = consolidated(workspace, capsys, cfg, "--policy", "require_agreement")
    assert summary["not_independent"] == 3
    assert {r["reviewer_independent"] for r in rows} == {"false"}
    assert all(r["agreement"] != "agree" and r["consolidated_from"] != "both" for r in rows)
    assert summary["by_consolidation"] == {"unresolved": 3}


def test_repeating_a_review_keeps_its_selection(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The audit's drift: metric levels change between two reviews of the same primary run."""
    selective = PRIMARY + (
        '[llm.review]\nmode = "selective"\ncriteria = ["metric_disagreement"]\n'
        'provider = "fake"\nmodel = "fake-reviewer"\n'
    )
    low = config(workspace, selective + SCORE_LOW)
    assert cli(workspace, capsys, "metrics", "run", cfg=low)[0] == 0
    primary(workspace, capsys, low)
    code, first = cli(workspace, capsys, "llm", "review", cfg=low)
    assert code == 0, first
    review = first["data"]["review"]
    scoring = review["metrics_scoring_run"]
    assert scoring is not None and review["frozen_from_review_run"] is None

    medium = config(workspace, selective + SCORE_MEDIUM)
    assert cli(workspace, capsys, "metrics", "run", cfg=medium)[0] == 0  # levels change
    code, second = cli(workspace, capsys, "llm", "review", cfg=medium)
    assert code == 0, second
    again = second["data"]["review"]
    assert second["data"]["run_id"] == first["data"]["run_id"]  # the same review run
    assert again["frozen_from_review_run"] == first["data"]["run_id"]
    assert again["selected"] == review["selected"] and again["metrics_scoring_run"] == scoring
    assert links(workspace) == [(1, first["data"]["run_id"])]

    # a new selection only when asked for explicitly
    code, third = cli(workspace, capsys, "llm", "review", "--reselect", cfg=medium)
    assert code == 0, third
    data = third["data"]
    if "review" in data:  # a fresh selection with the new levels
        assert data["review"]["frozen_from_review_run"] is None
        assert data["review"]["metrics_scoring_run"] != scoring
    else:  # the new levels select nothing: nothing planned
        assert data["selected"] == 0


def test_text_output_separates_reviewer_and_primary_costs(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from whatsapp_analyzer.cli.app import main  # noqa: PLC0415

    cfg = config(workspace, FAKE_DUAL)
    primary(workspace, capsys, cfg)
    assert main(["llm", "review", "-w", str(workspace), "--config", str(cfg)]) == 0
    text = capsys.readouterr().out
    assert "of primary run 1" in text and "reviewer, this run only" in text
    out = workspace.parent / "c.csv"
    assert (
        main(["llm", "consolidate", "-w", str(workspace), "--config", str(cfg), "--out", str(out)])
        == 0
    )
    text = capsys.readouterr().out
    assert "primary run 1:" in text and "reviewer run 2:" in text


def test_explicit_keys_never_become_the_frozen_criteria_selection(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = PRIMARY + (
        '[llm.review]\nmode = "selective"\ncriteria = ["review_required"]\n'
        'provider = "fake"\nmodel = "fake-reviewer"\n'
    )
    cfg = config(workspace, text)
    primary(workspace, capsys, cfg)
    assert cli(workspace, capsys, "llm", "review", "--conversation", "b", cfg=cfg)[0] == 0
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)  # criteria only
    assert code == 0 and payload["data"]["selected"] == 0  # not the {b} of the explicit review


def test_frozen_scope_follows_the_review_run_used_last(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from whatsapp_analyzer.config import ReviewCriterion, ReviewMode  # noqa: PLC0415
    from whatsapp_analyzer.runner import review  # noqa: PLC0415

    cfg = config(workspace, FAKE_DUAL)
    first = primary(workspace, capsys, cfg)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 0
    review_run = payload["data"]["run_id"]
    connection = connect(workspace / DATABASE_FILE)
    key = connection.execute("SELECT run_key FROM llm_runs WHERE id = ?", (review_run,)).fetchone()[
        0
    ]
    frozen = review.previous_selection(
        connection, first, ReviewMode.DUAL, (ReviewCriterion.REVIEW_REQUIRED,), key
    )
    connection.close()
    assert frozen is not None and frozen.frozen_from == review_run and len(frozen.version_ids) == 3


def test_a_review_run_is_not_a_primary_run(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, FAKE_DUAL)
    primary(workspace, capsys, cfg)
    code, payload = cli(workspace, capsys, "llm", "review", cfg=cfg)
    assert code == 0
    code, payload = cli(
        workspace, capsys, "llm", "review", "--primary-run", str(payload["data"]["run_id"]), cfg=cfg
    )
    assert code == 2 and payload["error"]["code"] == "PRIMARY_IS_A_REVIEW"


# Codex follow-up: selections sharing a review run, and the review used last -----------------

HIGH_FOR_ALL = (
    "[scoring.thresholds]\nhigh = 0.01\nmedium = 0.001\n"
    "[scoring]\nhigh_min_inbound_content_messages = 0\n"
)
SELECTIVE_METRIC = PRIMARY + (
    '[llm.review]\nmode = "selective"\ncriteria = ["metric_disagreement"]\n'
    'provider = "fake"\nmodel = "fake-reviewer"\n'
)
DUAL_REVIEW = PRIMARY + '[llm.review]\nmode = "dual"\nprovider = "fake"\nmodel = "fake-reviewer"\n'


def review_links(root: Path) -> list[tuple[Any, ...]]:
    connection = connect(root / DATABASE_FILE)
    try:
        return [
            tuple(r)
            for r in connection.execute(
                "SELECT review_run_id, mode, used_seq FROM llm_reviews ORDER BY id"
            )
        ]
    finally:
        connection.close()


def test_dual_then_selective_keep_their_own_selections(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A selective review that picks every conversation shares the dual review run, but keeps
    a selective link of its own, so its scope stays frozen when metrics change."""
    dual = config(workspace, DUAL_REVIEW + SCORE_LOW)
    assert cli(workspace, capsys, "metrics", "run", cfg=dual)[0] == 0
    primary(workspace, capsys, dual)
    code, d = cli(workspace, capsys, "llm", "review", cfg=dual)
    assert code == 0

    selective = config(workspace, SELECTIVE_METRIC + HIGH_FOR_ALL)
    assert cli(workspace, capsys, "metrics", "run", cfg=selective)[0] == 0
    code, s = cli(workspace, capsys, "llm", "review", cfg=selective)
    assert code == 0, s
    assert s["data"]["run_id"] == d["data"]["run_id"]  # same versions: the same review run
    scoring = s["data"]["review"]["metrics_scoring_run"]
    assert [link[:2] for link in review_links(workspace)] == [
        (d["data"]["run_id"], "dual"),
        (d["data"]["run_id"], "selective"),
    ]

    medium = config(workspace, SELECTIVE_METRIC + SCORE_MEDIUM)
    assert cli(workspace, capsys, "metrics", "run", cfg=medium)[0] == 0  # levels change
    code, again = cli(workspace, capsys, "llm", "review", cfg=medium)
    assert code == 0, again
    review = again["data"]["review"]
    assert review["mode"] == "selective" and review["frozen_from_review_run"] == d["data"]["run_id"]
    assert (
        review["metrics_scoring_run"] == scoring
        and review["selected"] == s["data"]["review"]["selected"]
    )

    # the dual selection is still its own, too (config() rewrites the same file)
    dual = config(workspace, DUAL_REVIEW + SCORE_LOW)
    code, dual_again = cli(workspace, capsys, "llm", "review", cfg=dual)
    assert code == 0 and dual_again["data"]["review"]["mode"] == "dual"


def test_default_consolidation_follows_the_review_used_last(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Going back to an earlier selection with --reselect makes it the default consolidation,
    with the metric reference of that selection; the order of creation does not matter."""
    low = config(workspace, SELECTIVE_METRIC + SCORE_LOW)
    assert cli(workspace, capsys, "metrics", "run", cfg=low)[0] == 0
    primary(workspace, capsys, low)
    code, a = cli(workspace, capsys, "llm", "review", cfg=low)
    assert code == 0, a

    high = config(workspace, SELECTIVE_METRIC + HIGH_FOR_ALL)
    assert cli(workspace, capsys, "metrics", "run", cfg=high)[0] == 0
    code, b = cli(workspace, capsys, "llm", "review", "--reselect", cfg=high)
    assert code == 0, b
    summary, _ = consolidated(workspace, capsys, high)
    assert summary["review_run"]["id"] == b["data"]["run_id"]

    assert cli(workspace, capsys, "metrics", "run", cfg=low)[0] == 0
    code, a2 = cli(workspace, capsys, "llm", "review", "--reselect", cfg=low)
    assert code == 0, a2
    assert a2["data"]["run_id"] == a["data"]["run_id"]  # back to the first scope
    summary, rows = consolidated(workspace, capsys, low)
    assert summary["review_run"]["id"] == a["data"]["run_id"]
    assert summary["metrics_scoring_run"] == a2["data"]["review"]["metrics_scoring_run"]
    # deterministic: consolidating again gives the same review
    again, rows_again = consolidated(workspace, capsys, low)
    assert again["review_run"]["id"] == a["data"]["run_id"] and rows_again == rows
    # an explicit run still wins
    summary, _ = consolidated(workspace, capsys, low, "--review-run", str(b["data"]["run_id"]))
    assert summary["review_run"]["id"] == b["data"]["run_id"]


def test_resuming_a_review_makes_it_the_latest(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config(workspace, DUAL_REVIEW)
    first = primary(workspace, capsys, cfg)
    code, payload = cli(workspace, capsys, "llm", "run", "--force", cfg=cfg)
    assert code == 0
    second = payload["data"]["run_id"]
    assert cli(workspace, capsys, "llm", "review", "--primary-run", str(first), cfg=cfg)[0] == 0
    assert cli(workspace, capsys, "llm", "review", "--primary-run", str(second), cfg=cfg)[0] == 0
    summary, _ = consolidated(workspace, capsys, cfg)
    assert summary["primary_run"]["id"] == second
    assert cli(workspace, capsys, "llm", "review", "--primary-run", str(first), cfg=cfg)[0] == 0
    summary, _ = consolidated(workspace, capsys, cfg)
    assert summary["primary_run"]["id"] == first  # the review used last, not the newest row
    seqs = [link[2] for link in review_links(workspace)]
    assert len(seqs) == len(set(seqs))
