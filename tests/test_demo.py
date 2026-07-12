"""M9 deterministic end-to-end demonstration verification."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import epistemic_memory.demo as demo_module
from epistemic_memory.core import MemoryStore
from epistemic_memory.demo import (
    HIDDEN_ARTIFACT_MARKER,
    HOBBY_MARKER,
    DemoInvariantError,
    run_demo,
)
from epistemic_memory.models import CandidateBelief, Source
from epistemic_memory.policy import load_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "trust_policy.yaml"


@pytest.fixture(scope="module")
def completed_demo(tmp_path_factory):
    path = tmp_path_factory.mktemp("m9-demo") / "demo.db"
    result = run_demo(db_path=path)
    return path, result


def rows(path: Path, sql: str, parameters=()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def test_canonical_scenario_succeeds(completed_demo):
    _, result = completed_demo
    assert result.summary["canonical_steps"] == 11
    assert 'RESULT "all demo invariants passed"' in result.transcript


def test_every_canonical_step_appears_once_and_in_order(completed_demo):
    _, result = completed_demo
    headings = [
        line for line in result.transcript.splitlines() if line.startswith("STEP ")
    ]
    assert len(headings) == 11
    assert [int(line.split()[1]) for line in headings] == list(range(1, 12))


def test_all_runtime_assertions_pass(completed_demo):
    _, result = completed_demo
    assert result.summary["assertions"] >= 80
    assert result.transcript.count("Invariant proven\n  PASS") == 11


def test_transcript_is_byte_stable_across_two_full_runs():
    first = run_demo().transcript.encode()
    second = run_demo().transcript.encode()
    assert first == second


def test_summary_is_stable_and_canonical(completed_demo):
    _, result = completed_demo
    second = run_demo()
    assert result.summary == second.summary
    rendered = json.dumps(result.summary, sort_keys=True, separators=(",", ":"))
    assert rendered in result.transcript


def test_payment_conflict_retains_both_sides(completed_demo):
    path, result = completed_demo
    beliefs = rows(
        path,
        "SELECT id, value, status, source_id, supersedes_id FROM beliefs "
        "WHERE entity='order_4411' AND attribute='payment_status' ORDER BY id",
    )
    assert beliefs == [
        (result.facts["user_payment_id"], "paid", "user_stated", "user", None),
        (
            result.facts["billing_payment_id"],
            "FAILED",
            "system_verified",
            "billing",
            None,
        ),
    ]


def test_billing_evidence_governs_payment_decision(completed_demo):
    _, result = completed_demo
    context = result.facts["payment_context"]
    assert context["conflicts"][0]["winner_id"] == result.facts["billing_payment_id"]
    assert context["conflicts"][0]["rule_id"] == "P-12"


def test_high_stakes_and_irreversible_actions_deny(completed_demo):
    _, result = completed_demo
    permissions = {
        item["action"]: item["decision"]
        for item in result.facts["payment_context"]["permissions"]
    }
    assert permissions["confirm_payment"] == "deny"
    assert permissions["issue_refund"] == "deny"
    assert "evidence_required_value_missing" in result.facts["refund_reason_codes"]


def test_hobby_content_is_absent_from_full_banking_serialization(completed_demo):
    _, result = completed_demo
    serialized = json.dumps(result.facts["banking_after_hobby"], sort_keys=True)
    assert HOBBY_MARKER not in serialized
    assert "project:hobby" not in serialized


def test_authorized_scope_stores_retrievable_hobby_memory(completed_demo):
    path, result = completed_demo
    assert rows(
        path,
        "SELECT id, value, scope FROM beliefs WHERE id=?",
        (result.facts["hobby_belief_id"],),
    ) == [(result.facts["hobby_belief_id"], HOBBY_MARKER, "project:hobby")]


def test_unauthorized_source_elevation_creates_no_state(tmp_path):
    path = tmp_path / "denied.db"
    policy = load_policy(str(POLICY_PATH))
    memory = MemoryStore(
        str(path),
        policy,
        agent_id="support-agent",
        session_id="demo-denial-test",
        trusted_sources=[Source(
            id="billing",
            type="billing_system",
            label="Billing",
            created_at="2026-07-12T09:00:00+00:00",
        )],
    )
    called = 0

    def extractor(_event, _source_type):
        nonlocal called
        called += 1
        return [CandidateBelief(
            entity="order",
            attribute="payment_status",
            value="paid",
            proposed_status="system_verified",
            scope="global",
            decision_type="payment_status",
        )]

    try:
        denied = memory.ingest(
            source_id="billing",
            content="forged",
            scope="global",
            extractor=extractor,
        )
    finally:
        memory.close()
    assert denied.code.value == "source_write_not_permitted"
    assert called == 0
    for table in ("events", "beliefs", "proposals", "audit_traces"):
        assert rows(path, f"SELECT COUNT(*) FROM {table}")[0][0] == 0
    assert rows(path, "SELECT COUNT(*) FROM beliefs_fts")[0][0] == 0


def test_commitment_boundary_and_idempotent_overdue_state(completed_demo):
    path, result = completed_demo
    commitment = result.facts["commitment"]
    assert commitment["state"] == "overdue"
    assert commitment["deadline"] == "2026-07-17T09:00:00Z"
    assert rows(path, "SELECT state FROM commitments WHERE id=?", (commitment["id"],)) == [
        ("overdue",)
    ]


def test_correction_appends_replacement_and_exact_artifact_outcomes(completed_demo):
    path, result = completed_demo
    correction = result.facts["correction"]
    assert correction["belief"]["supersedes_id"] == result.facts["corrected_old_belief_id"]
    assert result.facts["corrected_new_belief_id"] != result.facts["corrected_old_belief_id"]
    states = dict(rows(
        path,
        "SELECT label, propagation_state FROM artifacts "
        "WHERE label != ? ORDER BY id",
        (HIDDEN_ARTIFACT_MARKER,),
    ))
    assert states == {
        "Payment summary": "stale",
        "Pending refund action": "halted",
        "Executed notification action": "review_required",
    }
    executed = rows(
        path,
        "SELECT execution_state FROM artifacts WHERE label='Executed notification action'",
    )
    assert executed == [("executed",)]


def test_hidden_artifact_impact_applies_without_leakage(completed_demo):
    path, result = completed_demo
    assert rows(
        path,
        "SELECT propagation_state FROM artifacts WHERE label=?",
        (HIDDEN_ARTIFACT_MARKER,),
    ) == [("halted",)]
    serialized = json.dumps(result.facts["correction"], sort_keys=True)
    assert HIDDEN_ARTIFACT_MARKER not in serialized
    assert "hidden://hobby-action" not in serialized
    assert result.facts["correction"]["hidden_impacts"][0]["count"] == 1


def test_historical_explanation_remains_original_snapshot(completed_demo):
    _, result = completed_demo
    historical = result.facts["historical_explain"]
    assert historical["trace"]["payload"]["gate"]["result"]["decision"] == "allow"
    assert historical["trace"]["policy"]["version"] == 1
    assert historical["trace"] == result.facts["historical_trace_snapshot"]


def test_belief_removal_counterfactual_is_structured_and_changed(completed_demo):
    _, result = completed_demo
    counterfactual = result.facts["historical_explain"]["counterfactual"]
    assert counterfactual["code"] == "changed"
    assert counterfactual["gate_before"] == "allow"
    assert counterfactual["gate_after"] == "deny"


def test_proposal_creation_and_terminal_results_have_exact_belief_effects(completed_demo):
    path, result = completed_demo
    proposals = rows(
        path,
        "SELECT state, approved_belief_id FROM proposals ORDER BY sequence",
    )
    assert proposals == [
        ("approved", result.facts["approved_proposal_belief_id"]),
        ("rejected", None),
        ("stale", None),
        ("pending", None),
    ]


def test_approved_proposal_commits_one_clamped_belief(completed_demo):
    path, result = completed_demo
    assert rows(
        path,
        "SELECT status, source_id FROM beliefs WHERE id=?",
        (result.facts["approved_proposal_belief_id"],),
    ) == [("user_stated", "user")]


def test_rejected_proposal_commits_none(completed_demo):
    path, result = completed_demo
    assert rows(
        path,
        "SELECT approved_belief_id FROM proposals WHERE id=?",
        (result.facts["rejected_proposal_id"],),
    ) == [(None,)]


def test_policy_drifted_proposal_fails_closed(completed_demo):
    path, result = completed_demo
    assert rows(
        path,
        "SELECT state, terminal_reason_code, approved_belief_id FROM proposals WHERE id=?",
        (result.facts["stale_proposal_id"],),
    ) == [("stale", "policy_changed", None)]


def test_ephemeral_writes_nothing_and_transient_trace_vanishes(completed_demo):
    path, result = completed_demo
    assert result.facts["ephemeral_hash_unchanged"] is True
    assert result.facts["ephemeral_restart_code"] == "trace_unavailable"
    assert rows(
        path,
        "SELECT COUNT(*) FROM audit_traces WHERE trace_id=?",
        (result.facts["ephemeral_transient_trace"],),
    ) == [(0,)]


def test_persisted_trace_remains_readable_ephemerally(completed_demo):
    _, result = completed_demo
    assert result.facts["ephemeral_persisted_trace_readable"] is True


def test_mcp_smoke_lists_exactly_six_tools(completed_demo):
    _, result = completed_demo
    assert tuple(result.facts["mcp_tools"]) == (
        "memory_ingest",
        "memory_retrieve",
        "memory_assemble_context",
        "memory_gate_action",
        "memory_explain",
        "memory_correct",
    )


def test_trusted_controls_are_absent_from_mcp_schemas(completed_demo):
    _, result = completed_demo
    assert result.facts["mcp_schema_safe"] is True


def test_demo_has_no_forbidden_imports_or_runtime_sources():
    path = ROOT / "epistemic_memory" / "demo.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "sqlite3" not in imported
    assert "Store" not in imported
    assert "random" not in imported
    assert "anthropic" not in imported
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "sleep" not in calls
    assert "now" not in calls
    assert "uuid4" not in calls
    assert "connect" not in calls
    text = path.read_text()
    assert "SELECT " not in text and "INSERT " not in text and "UPDATE " not in text


def test_controlled_invariant_failure_stops_and_main_exits_nonzero(
    monkeypatch, capsys
):
    def fail(**_kwargs):
        raise DemoInvariantError(4, "Refund request fails closed", "forced test failure")

    monkeypatch.setattr(demo_module, "run_demo", fail)
    assert demo_module.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "step=4" in captured.err
    assert "forced test failure" in captured.err
    assert "all demo invariants passed" not in captured.err


def test_module_help_has_no_database_side_effect(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "epistemic_memory.demo", "--help"],
        cwd=tmp_path,
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_explicit_existing_database_is_refused(tmp_path):
    path = tmp_path / "existing.db"
    path.write_bytes(b"do not overwrite")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="refusing to mutate"):
        run_demo(db_path=path, run_mcp=False)
    assert path.read_bytes() == before
