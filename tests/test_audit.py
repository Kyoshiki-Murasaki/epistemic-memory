"""M7 immutable audit traces, historical explain, and atomic coverage."""

from __future__ import annotations

import ast
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from epistemic_memory.audit import policy_snapshot
import epistemic_memory.core as core_module
from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    ArtifactExecutionState,
    ArtifactKind,
    ArtifactRegistrationRequest,
    AssemblyRequest,
    AuditOperation,
    AuditResultCode,
    CandidateBelief,
    CommitmentCreateRequest,
    CommitmentTransitionRequest,
    CorrectionKind,
    CorrectionRequest,
    CounterfactualCode,
    DependencyEndpointKind,
    DependencyRegistrationRequest,
    EpistemicStatus,
    ExplainRequest,
    ExplainResultCode,
    GateDecision,
    IngestResultCode,
    MemoryOperation,
    RetrievalRequest,
    Source,
    StructuralFollowUpCode,
    TrustPolicy,
)
from epistemic_memory.policy import load_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = str(ROOT / "epistemic_memory" / "trust_policy.yaml")
NOW = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
GOLDEN = Path(__file__).parent / "golden" / "m7_explain_payment_conflict.txt"


class FixedClock:
    def __init__(self, instant: datetime = NOW):
        self.instant = instant
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.instant

    def set(self, instant: datetime) -> None:
        self.instant = instant


class SequenceIds:
    def __init__(self, prefix: str = "id"):
        self.prefix = prefix
        self.calls: list[str] = []

    def __call__(self, kind: str) -> str:
        self.calls.append(kind)
        return f"{self.prefix}-{kind}-{len(self.calls):03d}"


def extractor_for(
    *,
    entity: str = "order_4411",
    attribute: str = "payment_status",
    value: str = "paid",
    status: EpistemicStatus = EpistemicStatus.ai_inferred,
    scope: str = "global",
    decision_type: str | None = "payment_status",
):
    def extractor(event, source_type):
        return [CandidateBelief(
            entity=entity,
            attribute=attribute,
            value=value,
            proposed_status=status,
            scope=scope,
            decision_type=decision_type,
        )]

    return extractor


@pytest.fixture
def m7(tmp_path, policy_with_ingest_sources):
    policy = policy_with_ingest_sources(
        load_policy(POLICY_PATH),
        "support-agent",
        {
            "user": "user",
            "support-agent": "agent_inference",
            "billing": "billing_system",
            "billing2": "billing_system",
        },
    )
    clock = FixedClock()
    ids = SequenceIds()
    db_path = str(tmp_path / "m7.db")
    memory = MemoryStore(
        db_path,
        policy,
        agent_id="support-agent",
        session_id="session-audit-001",
        clock=clock,
        id_factory=ids,
    )
    for source_id, source_type, label in [
        ("support-agent", "agent_inference", "Support agent inference"),
        ("user", "user", "Customer chat"),
        ("billing", "billing_system", "Billing system"),
        ("billing2", "billing_system", "Billing system 2"),
    ]:
        memory._store.add_source(Source(
            id=source_id,
            type=source_type,
            label=label,
            created_at=NOW.isoformat(),
        ))
    yield memory, policy, clock, ids, db_path
    memory.close()


def ingest(
    memory: MemoryStore,
    *,
    source_id: str = "support-agent",
    content: str = "candidate",
    **candidate,
):
    return memory.ingest(
        source_id=source_id,
        content=content,
        scope=candidate.get("scope", "global"),
        extractor=extractor_for(**candidate),
    )


def trace(memory: MemoryStore, trace_id: str):
    value = memory._store.get_audit_trace(trace_id)
    assert value is not None
    return value


def test_assembled_response_trace_captures_exact_m4_structures(m7):
    memory, policy, _, _, _ = m7
    user = ingest(
        memory,
        source_id="user",
        content="I paid",
        status=EpistemicStatus.user_stated,
    ).beliefs[0]
    billing = ingest(
        memory,
        source_id="billing",
        content="payment failed",
        value="FAILED",
        status=EpistemicStatus.system_verified,
    ).beliefs[0]

    result = memory.assemble(AssemblyRequest(scope="global", token_budget=2000))
    stored = trace(memory, result.trace_id)
    snapshot = stored.payload.assembly

    assert result.ok is True
    assert result.trace_persisted is True
    assert stored.operation == AuditOperation.assemble
    assert stored.agent_id == "support-agent"
    assert stored.active_scope == "global"
    assert stored.policy == policy_snapshot(policy)
    assert [item.evidence.belief_id for item in snapshot.evidence] == [
        item.belief.id for item in result.items
    ] == [billing.id, user.id]
    assert snapshot.conflicts == result.conflicts
    assert snapshot.permissions == result.permissions
    assert snapshot.receipt == result.receipt
    assert snapshot.rendered_receipt == result.rendered_receipt
    assert snapshot.tokens_injected == result.tokens_injected
    assert snapshot.token_budget == result.token_budget
    assert stored.payload.counterfactuals


def test_gate_trace_captures_evidence_tier_decision_reasons_and_rules(m7):
    memory, _, _, _, _ = m7
    billing = ingest(
        memory,
        source_id="billing",
        value="FAILED",
        status=EpistemicStatus.system_verified,
    ).beliefs[0]

    result = memory.gate(
        action="issue_refund", entity="order_4411", scope="global"
    )
    stored = trace(memory, result.trace_id)
    snapshot = stored.payload.gate

    assert result.decision == GateDecision.deny
    assert result.trace_persisted is True
    assert snapshot.action == "issue_refund"
    assert snapshot.result.decision == result.decision
    assert snapshot.result.risk_tier.value == "irreversible"
    assert snapshot.result.reason_codes == result.reason_codes
    assert snapshot.result.rule_ids == result.rule_ids
    assert [item.belief_id for item in snapshot.evidence] == [billing.id]
    assert snapshot.evidence[0].source_type == "billing_system"


def test_direct_ingest_event_beliefs_and_trace_are_atomic_and_attributable(m7):
    memory, _, _, ids, _ = m7
    result = ingest(memory, value="paid")

    assert result.ok is True
    assert result.code == IngestResultCode.beliefs_committed
    assert result.trace_id
    stored = trace(memory, result.trace_id)
    assert stored.payload.mutation.event_ids == [result.event.id]
    assert stored.payload.mutation.belief_ids == [result.beliefs[0].id]
    assert stored.payload.mutation.beliefs[0].value == "paid"
    assert stored.agent_id == "support-agent"
    assert ids.calls == ["trace"]
    assert memory._store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert memory._store.conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == 1
    assert memory._store.conn.execute("SELECT COUNT(*) FROM audit_traces").fetchone()[0] == 1


def test_audit_trace_update_delete_are_blocked_and_order_is_sequence(m7):
    memory, _, clock, _, _ = m7
    first = ingest(memory, value="first")
    clock.set(NOW)
    second = ingest(memory, value="second")

    ordered = memory._store.list_audit_traces()
    assert [item.trace_id for item in ordered] == [first.trace_id, second.trace_id]
    assert [item.sequence for item in ordered] == [1, 2]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        memory._store.conn.execute(
            "UPDATE audit_traces SET result_code = 'forged' WHERE trace_id = ?",
            (first.trace_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        memory._store.conn.execute(
            "DELETE FROM audit_traces WHERE trace_id = ?", (first.trace_id,)
        )


def test_direct_ingest_audit_failure_rolls_back_event_belief_fts_and_trace(
    m7, monkeypatch
):
    memory, _, _, _, _ = m7
    original = memory._store.add_audit_trace

    def insert_then_fail(value):
        original(value)
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(memory._store, "add_audit_trace", insert_then_fail)
    result = ingest(memory)

    assert result.code == IngestResultCode.audit_persistence_failed
    for table in ("events", "beliefs", "beliefs_fts", "audit_traces"):
        assert memory._store.conn.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["assemble", "gate"])
def test_response_audit_failure_returns_no_untraced_success(m7, monkeypatch, operation):
    memory, _, _, _, _ = m7
    ingest(memory)

    def fail(_trace):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(memory._store, "add_audit_trace", fail)
    before = memory._store.conn.execute("SELECT COUNT(*) FROM audit_traces").fetchone()[0]
    if operation == "assemble":
        result = memory.assemble(AssemblyRequest(scope="global", token_budget=2000))
        assert result.ok is False
        assert result.result_code == AuditResultCode.audit_persistence_failed
        assert result.text == ""
        assert result.receipt is None
    else:
        result = memory.gate(
            action="ask_for_receipt", entity="order_4411", scope="global"
        )
        assert result.ok is False
        assert result.result_code == AuditResultCode.audit_persistence_failed
        assert result.decision == GateDecision.deny
        assert result.trace_id is None
    assert memory._store.conn.execute("SELECT COUNT(*) FROM audit_traces").fetchone()[0] == before


def test_commitment_artifact_dependency_and_correction_mutations_are_traced(m7):
    memory, _, clock, _, _ = m7
    original = ingest(memory).beliefs[0]
    artifact = memory.register_artifact(ArtifactRegistrationRequest(
        kind=ArtifactKind.output,
        execution_state=ArtifactExecutionState.not_applicable,
        scope="global",
        label="Generated answer",
    ))
    dependency = memory.register_dependency(DependencyRegistrationRequest(
        upstream_kind=DependencyEndpointKind.belief,
        upstream_id=original.id,
        downstream_artifact_id=artifact.artifact.id,
        scope="global",
    ))
    commitment = memory.add_commitment(CommitmentCreateRequest(
        description="Follow up",
        owner="support",
        beneficiary="customer",
        scope="global",
        deadline=NOW + timedelta(days=1),
        preconditions=[],
    ))
    clock.set(NOW + timedelta(hours=1))
    transitioned = memory.transition_commitment(CommitmentTransitionRequest(
        commitment_id=commitment.commitment.id,
        target_state="waiting",
        scope="global",
    ))
    corrected = memory.correct(CorrectionRequest(
        belief_id=original.id,
        kind=CorrectionKind.correction,
        content="corrected",
        scope="global",
        value="unpaid",
        proposed_status=EpistemicStatus.ai_inferred,
    ))

    results = [artifact, dependency, commitment, transitioned, corrected]
    assert all(result.trace_id for result in results)
    operations = [trace(memory, result.trace_id).operation for result in results]
    assert operations == [
        AuditOperation.artifact_register,
        AuditOperation.dependency_register,
        AuditOperation.commitment_create,
        AuditOperation.commitment_transition,
        AuditOperation.correction,
    ]
    assert corrected.visible_impacts[0].artifact.id == artifact.artifact.id


def test_correction_trace_retains_only_safe_hidden_impact_aggregate(m7):
    memory, _, _, _, _ = m7
    original = ingest(memory).beliefs[0]
    visible = memory.register_artifact(ArtifactRegistrationRequest(
        kind="output",
        execution_state="not_applicable",
        scope="global",
        label="visible",
    )).artifact
    hidden_marker = "HIDDEN_ARTIFACT_LABEL_MARKER"
    hidden = memory.register_artifact(ArtifactRegistrationRequest(
        kind="action",
        execution_state="pending",
        scope="project:hobby",
        label=hidden_marker,
        reference="secret://hidden",
    )).artifact
    assert memory.register_dependency(DependencyRegistrationRequest(
        upstream_kind="belief",
        upstream_id=original.id,
        downstream_artifact_id=visible.id,
        scope="global",
    )).ok
    assert memory.register_dependency(DependencyRegistrationRequest(
        upstream_kind="artifact",
        upstream_id=visible.id,
        downstream_artifact_id=hidden.id,
        scope="project:hobby",
    )).ok

    result = memory.correct(CorrectionRequest(
        belief_id=original.id,
        kind="correction",
        content="correct",
        scope="project:banking",
        value="unpaid",
        proposed_status="ai_inferred",
    ))
    stored = trace(memory, result.trace_id)
    serialized = stored.model_dump_json()

    assert result.hidden_impacts[0].count == 1
    assert stored.payload.mutation.hidden_impact_count == 1
    assert stored.payload.mutation.visible_impact_artifact_ids == [visible.id]
    assert hidden_marker not in serialized
    assert "secret://hidden" not in serialized
    assert "project:hobby" not in serialized


def test_correction_audit_failure_rolls_back_belief_event_and_propagation(
    m7, monkeypatch
):
    memory, _, _, _, _ = m7
    original = ingest(memory).beliefs[0]
    artifact = memory.register_artifact(ArtifactRegistrationRequest(
        kind="output",
        execution_state="not_applicable",
        scope="global",
        label="answer",
    )).artifact
    memory.register_dependency(DependencyRegistrationRequest(
        upstream_kind="belief",
        upstream_id=original.id,
        downstream_artifact_id=artifact.id,
        scope="global",
    ))
    before = {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("events", "beliefs", "audit_traces")
    }

    def fail(_trace):
        raise RuntimeError("injected")

    monkeypatch.setattr(memory._store, "add_audit_trace", fail)
    result = memory.correct(CorrectionRequest(
        belief_id=original.id,
        kind="correction",
        content="correct",
        scope="global",
        value="unpaid",
        proposed_status="ai_inferred",
    ))

    assert result.code.value == "audit_persistence_failed"
    assert memory._store.is_current(original.id) is True
    assert memory._store.get_artifact(artifact.id).propagation_state.value == "current"
    assert {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    } == before


def test_commitment_creation_audit_failure_rolls_back_domain_row_and_trace(
    m7, monkeypatch
):
    memory, _, _, _, _ = m7
    before = {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("commitments", "audit_traces")
    }
    monkeypatch.setattr(
        memory._store, "add_audit_trace", lambda _trace: (_ for _ in ()).throw(
            RuntimeError("injected")
        )
    )

    result = memory.add_commitment(CommitmentCreateRequest(
        description="Call customer",
        owner="support",
        beneficiary="customer",
        scope="global",
        deadline=NOW + timedelta(days=1),
        preconditions=[],
    ))

    assert result.code.value == "audit_persistence_failed"
    assert result.commitment is None
    assert {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    } == before


def test_commitment_transition_audit_failure_restores_state_and_trace_count(
    m7, monkeypatch
):
    memory, _, clock, _, _ = m7
    created = memory.add_commitment(CommitmentCreateRequest(
        description="Call customer",
        owner="support",
        beneficiary="customer",
        scope="global",
        deadline=NOW + timedelta(days=1),
        preconditions=[],
    ))
    before_trace_count = memory._store.conn.execute(
        "SELECT COUNT(*) FROM audit_traces"
    ).fetchone()[0]
    clock.set(NOW + timedelta(hours=1))
    monkeypatch.setattr(
        memory._store, "add_audit_trace", lambda _trace: (_ for _ in ()).throw(
            RuntimeError("injected")
        )
    )

    result = memory.transition_commitment(CommitmentTransitionRequest(
        commitment_id=created.commitment.id,
        target_state="waiting",
        scope="global",
    ))

    assert result.code.value == "audit_persistence_failed"
    assert memory._store.get_commitment(created.commitment.id).state.value == "open"
    assert memory._store.conn.execute(
        "SELECT COUNT(*) FROM audit_traces"
    ).fetchone()[0] == before_trace_count


def test_multi_commitment_overdue_audit_failure_is_all_or_nothing(m7, monkeypatch):
    memory, _, clock, _, _ = m7
    commitment_ids = []
    for description in ("First", "Second"):
        created = memory.add_commitment(CommitmentCreateRequest(
            description=description,
            owner="support",
            beneficiary="customer",
            scope="global",
            deadline=NOW + timedelta(hours=1),
            preconditions=[],
        ))
        commitment_ids.append(created.commitment.id)
    before_trace_count = memory._store.conn.execute(
        "SELECT COUNT(*) FROM audit_traces"
    ).fetchone()[0]
    clock.set(NOW + timedelta(days=1))
    monkeypatch.setattr(
        memory._store, "add_audit_trace", lambda _trace: (_ for _ in ()).throw(
            RuntimeError("injected")
        )
    )

    result = memory.surface_overdue(
        core_module.OverdueScanRequest(scope="global")
    )

    assert result.code.value == "audit_persistence_failed"
    assert result.promoted_count == 0
    assert [
        memory._store.get_commitment(value).state.value for value in commitment_ids
    ] == ["open", "open"]
    assert memory._store.conn.execute(
        "SELECT COUNT(*) FROM audit_traces"
    ).fetchone()[0] == before_trace_count


def test_artifact_registration_audit_failure_rolls_back_row_and_trace(m7, monkeypatch):
    memory, _, _, _, _ = m7
    before = {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("artifacts", "audit_traces")
    }
    monkeypatch.setattr(
        memory._store, "add_audit_trace", lambda _trace: (_ for _ in ()).throw(
            RuntimeError("injected")
        )
    )

    result = memory.register_artifact(ArtifactRegistrationRequest(
        kind="output",
        execution_state="not_applicable",
        scope="global",
        label="must roll back",
    ))

    assert result.code.value == "audit_persistence_failed"
    assert result.artifact is None
    assert {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    } == before


def test_dependency_registration_audit_failure_rolls_back_row_and_trace(
    m7, monkeypatch
):
    memory, _, _, _, _ = m7
    belief = ingest(memory).beliefs[0]
    artifact = memory.register_artifact(ArtifactRegistrationRequest(
        kind="output",
        execution_state="not_applicable",
        scope="global",
        label="answer",
    )).artifact
    before = {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("dependencies", "audit_traces")
    }
    monkeypatch.setattr(
        memory._store, "add_audit_trace", lambda _trace: (_ for _ in ()).throw(
            RuntimeError("injected")
        )
    )

    result = memory.register_dependency(DependencyRegistrationRequest(
        upstream_kind="belief",
        upstream_id=belief.id,
        downstream_artifact_id=artifact.id,
        scope="global",
    ))

    assert result.code.value == "audit_persistence_failed"
    assert result.dependency is None
    assert {
        table: memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    } == before


def test_explain_historical_snapshot_and_counterfactual_ignore_later_policy_and_belief(m7):
    memory, policy, clock, _, _ = m7
    original = ingest(memory, value="paid").beliefs[0]
    assembled = memory.assemble(AssemblyRequest(
        entity="order_4411", scope="global", token_budget=2000
    ))
    before_trace = trace(memory, assembled.trace_id).model_dump(mode="json")
    before_cf = deepcopy(before_trace["payload"]["counterfactuals"])

    changed_policy = policy.model_copy(update={"version": 99})
    memory.policy = changed_policy
    clock.set(NOW + timedelta(days=1))
    corrected = memory.correct(CorrectionRequest(
        belief_id=original.id,
        kind="correction",
        content="later correction",
        scope="global",
        value="unpaid",
        proposed_status="ai_inferred",
    ))
    assert corrected.ok

    explained = memory.explain(ExplainRequest(
        trace_id=assembled.trace_id,
        scope="global",
        belief_id=original.id,
    ))

    assert explained.authorized is True
    assert explained.trace.policy.version == 1
    assert explained.trace.payload.assembly.evidence[0].evidence.value == "paid"
    assert explained.trace.payload.counterfactuals[0].model_dump(mode="json") == before_cf[0]
    assert explained.counterfactual.model_dump(mode="json") == before_cf[0]
    assert explained.current_follow_up[0].code == (
        StructuralFollowUpCode.superseded_by_later_same_source_belief
    )
    assert "CURRENT FOLLOW-UP (NOT PART OF ORIGINAL DECISION)" in explained.rendered


def test_explain_current_scope_changes_only_access_not_historical_reasoning(m7):
    memory, policy, clock, _, _ = m7
    support = policy.agents["support-agent"]
    trace_policy = policy.model_copy(update={
        "agents": {
            **policy.agents,
            "support-agent": support.model_copy(update={
                "allowed_scopes": ["global", "persona"]
            }),
        },
    })
    memory.policy = trace_policy
    original = ingest(memory, value="paid").beliefs[0]
    assembled = memory.assemble(AssemblyRequest(
        entity="order_4411", scope="global", token_budget=2000
    ))
    baseline = memory.explain(ExplainRequest(
        trace_id=assembled.trace_id, scope="global", belief_id=original.id
    ))
    historical = baseline.trace.model_dump(mode="json")

    memory.policy = trace_policy.model_copy(update={
        "version": 2,
        "agents": {
            **trace_policy.agents,
            "support-agent": support.model_copy(update={
                "allowed_scopes": ["global"]
            }),
        },
    })
    unrelated_edit = memory.explain(ExplainRequest(
        trace_id=assembled.trace_id, scope="global", belief_id=original.id
    ))
    assert unrelated_edit.trace.model_dump(mode="json") == historical
    assert unrelated_edit.rendered == baseline.rendered

    memory.policy = trace_policy.model_copy(update={
        "version": 3,
        "agents": {
            **trace_policy.agents,
            "support-agent": support.model_copy(update={
                "allowed_scopes": ["persona"]
            }),
        },
    })
    hidden = memory.explain(ExplainRequest(
        trace_id=assembled.trace_id, scope="global", belief_id=original.id
    ))
    assert hidden.authorized is False
    assert hidden.code == ExplainResultCode.trace_unavailable
    assert hidden.trace is None
    assert hidden.rendered == ""

    memory.policy = trace_policy
    restored = memory.explain(ExplainRequest(
        trace_id=assembled.trace_id, scope="global", belief_id=original.id
    ))
    assert restored.trace.model_dump(mode="json") == historical
    assert restored.rendered == baseline.rendered

    clock.set(NOW + timedelta(days=1))
    memory.policy = policy
    later_secret = "LATER_SCOPE_SECRET"
    later = ingest(
        memory,
        entity="hidden",
        attribute="note",
        value=later_secret,
        scope="project:hobby",
        decision_type=None,
    )
    assert later.ok
    expanded = memory.explain(ExplainRequest(
        trace_id=assembled.trace_id,
        scope="project:hobby",
        belief_id=original.id,
    ))
    assert expanded.trace.model_dump(mode="json") == historical
    assert later_secret not in expanded.model_dump_json()
    assert expanded.rendered == baseline.rendered


@pytest.mark.parametrize("operation", ["assemble", "gate"])
def test_durable_decision_trace_and_evaluation_are_one_writer_serialized_point(
    m7, monkeypatch, operation
):
    memory, policy, _, _, db_path = m7
    original = ingest(memory, value="paid").beliefs[0]
    evaluated = threading.Event()
    writer_ready = threading.Event()
    writer_attempted = threading.Event()
    writer_finished = threading.Event()

    if operation == "assemble":
        original_builder = core_module.build_assembly_trace

        def paused_builder(*args, **kwargs):
            built = original_builder(*args, **kwargs)
            evaluated.set()
            assert writer_attempted.wait(5)
            assert not writer_finished.is_set()
            return built

        monkeypatch.setattr(core_module, "build_assembly_trace", paused_builder)
    else:
        original_builder = core_module.build_gate_trace

        def paused_builder(*args, **kwargs):
            built = original_builder(*args, **kwargs)
            evaluated.set()
            assert writer_attempted.wait(5)
            assert not writer_finished.is_set()
            return built

        monkeypatch.setattr(core_module, "build_gate_trace", paused_builder)

    def correct_after_evaluation():
        writer = MemoryStore(
            db_path,
            policy,
            agent_id="support-agent",
            session_id=f"concurrent-{operation}",
            clock=FixedClock(NOW + timedelta(days=1)),
            id_factory=SequenceIds(f"concurrent-{operation}"),
        )
        try:
            writer_ready.set()
            assert evaluated.wait(5)
            original_transaction = writer._store.transaction

            @contextmanager
            def observed_transaction(*, immediate=False):
                writer_attempted.set()
                with original_transaction(immediate=immediate):
                    yield

            writer._store.transaction = observed_transaction
            return writer.correct(CorrectionRequest(
                belief_id=original.id,
                kind="correction",
                content="concurrent correction",
                scope="global",
                value="unpaid",
                proposed_status="ai_inferred",
            ))
        finally:
            writer_finished.set()
            writer.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        correction_future = pool.submit(correct_after_evaluation)
        assert writer_ready.wait(5)
        decision = (
            memory.assemble(AssemblyRequest(
                entity="order_4411", scope="global", token_budget=2000
            ))
            if operation == "assemble"
            else memory.gate(
                action="issue_refund", entity="order_4411", scope="global"
            )
        )
        correction = correction_future.result(timeout=5)

    assert decision.trace_id
    assert correction.ok
    decision_trace = trace(memory, decision.trace_id)
    correction_trace = trace(memory, correction.trace_id)
    assert decision_trace.sequence < correction_trace.sequence
    assert [item.value for item in (
        [entry.evidence for entry in decision_trace.payload.assembly.evidence]
        if operation == "assemble"
        else decision_trace.payload.gate.evidence
    )] == ["paid"]
    assert memory._store.is_current(original.id) is False


def test_explain_structural_follow_up_wording_distinguishes_required_states(m7):
    memory, _, clock, _, _ = m7
    still = ingest(memory, entity="case", attribute="signal", value="still",
                   decision_type=None).trace_id
    disputed = ingest(memory, entity="case", attribute="disputed", value="x",
                      status=EpistemicStatus.disputed, decision_type=None)
    do_not_use = ingest(memory, entity="case", attribute="blocked", value="x",
                        status=EpistemicStatus.do_not_use, decision_type=None)
    old = ingest(memory, entity="case", attribute="ordinary", value="old",
                 decision_type=None)
    clock.set(NOW + timedelta(hours=1))
    memory.correct(CorrectionRequest(
        belief_id=old.beliefs[0].id,
        kind="correction",
        content="new",
        scope="global",
        value="new",
        proposed_status="ai_inferred",
    ))
    retract_old = ingest(memory, entity="case", attribute="retracted", value="old",
                         decision_type=None)
    clock.set(NOW + timedelta(hours=2))
    retracted = memory.correct(CorrectionRequest(
        belief_id=retract_old.beliefs[0].id,
        kind="retraction",
        content="retract",
        scope="global",
    ))

    cases = [
        (still, StructuralFollowUpCode.still_structurally_current),
        (disputed.trace_id, StructuralFollowUpCode.current_disputed),
        (do_not_use.trace_id, StructuralFollowUpCode.current_do_not_use),
        (old.trace_id, StructuralFollowUpCode.superseded_by_later_same_source_belief),
        (retract_old.trace_id, StructuralFollowUpCode.superseded_by_retraction),
        (retracted.trace_id, StructuralFollowUpCode.current_retraction),
    ]
    for trace_id, expected in cases:
        result = memory.explain(ExplainRequest(trace_id=trace_id, scope="global"))
        assert result.current_follow_up[0].code == expected


def test_cross_source_conflict_is_never_structural_supersession(m7):
    memory, _, _, _, _ = m7
    user = ingest(memory, source_id="user", status=EpistemicStatus.user_stated)
    billing = ingest(
        memory,
        source_id="billing",
        value="FAILED",
        status=EpistemicStatus.system_verified,
    )
    assembled = memory.assemble(AssemblyRequest(scope="global", token_budget=2000))
    result = memory.explain(ExplainRequest(trace_id=assembled.trace_id, scope="global"))

    assert {item.belief_id for item in result.current_follow_up} == {
        user.beliefs[0].id,
        billing.beliefs[0].id,
    }
    assert all(
        item.code == StructuralFollowUpCode.still_structurally_current
        for item in result.current_follow_up
    )
    assert "superseded_by" not in result.rendered


def test_counterfactual_changed_no_change_and_not_applicable_are_structured(m7):
    memory, _, _, _, _ = m7
    billing = ingest(
        memory,
        source_id="billing",
        value="paid",
        status=EpistemicStatus.system_verified,
    )
    gate = memory.gate(action="issue_refund", entity="order_4411", scope="global")
    changed = memory.explain(ExplainRequest(
        trace_id=gate.trace_id,
        scope="global",
        belief_id=billing.beliefs[0].id,
    ))
    assert changed.counterfactual.code == CounterfactualCode.changed
    assert changed.counterfactual.gate_before == GateDecision.allow
    assert changed.counterfactual.gate_after == GateDecision.deny

    note = ingest(
        memory,
        entity="note",
        attribute="unconfigured",
        value="memo",
        decision_type=None,
    )
    assembly = memory.assemble(AssemblyRequest(
        entity="note", scope="global", token_budget=2000
    ))
    no_change = memory.explain(ExplainRequest(
        trace_id=assembly.trace_id,
        scope="global",
        belief_id=note.beliefs[0].id,
    ))
    assert no_change.counterfactual.code == CounterfactualCode.no_change

    mutation = memory.explain(ExplainRequest(
        trace_id=note.trace_id,
        scope="global",
        belief_id=note.beliefs[0].id,
    ))
    assert mutation.counterfactual.code == (
        CounterfactualCode.counterfactual_not_applicable
    )


def test_unknown_hidden_unauthorized_and_corrupt_traces_fail_closed_without_leak(m7):
    memory, policy, _, _, db_path = m7
    secret = "HIDDEN_TRACE_SECRET_MARKER"
    hidden_ingest = ingest(
        memory,
        entity="hidden",
        attribute="secret",
        value=secret,
        scope="project:hobby",
        decision_type=None,
    )
    hidden_trace = hidden_ingest.trace_id
    fingerprint = policy_snapshot(policy).fingerprint
    memory._store.conn.execute(
        "INSERT INTO audit_traces (trace_id, session_id, session_mode, agent_id, "
        "active_scope, operation, outcome, result_code, reason_codes, rule_ids, "
        "policy_version, policy_fingerprint, payload, persisted, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            "corrupt-trace",
            "session-corrupt",
            "direct",
            "support-agent",
            "global",
            "assemble",
            "completed",
            "context_assembled",
            "[]",
            "[]",
            policy.version,
            fingerprint,
            "{}",
            NOW.isoformat(),
        ),
    )
    memory._store.conn.commit()

    hidden = memory.explain(ExplainRequest(
        trace_id=hidden_trace, scope="project:banking"
    ))
    missing = memory.explain(ExplainRequest(
        trace_id="missing-trace", scope="project:banking"
    ))
    corrupt = memory.explain(ExplainRequest(
        trace_id="corrupt-trace", scope="global"
    ))
    no_explain_permissions = policy.model_copy(update={
        "agents": {
            **policy.agents,
            "no-explain": policy.agents["support-agent"].model_copy(update={
                "memory_operations": [MemoryOperation.propose]
            }),
        }
    })
    denied_store = MemoryStore(
        db_path,
        no_explain_permissions,
        agent_id="no-explain",
        session_id="session-denied",
    )
    try:
        denied = denied_store.explain(ExplainRequest(
            trace_id=hidden_trace, scope="project:hobby"
        ))
    finally:
        denied_store.close()

    for result in (hidden, missing, corrupt, denied):
        assert result.authorized is False
        assert result.code == ExplainResultCode.trace_unavailable
        assert result.trace is None
        assert result.rendered == ""
        assert secret not in result.model_dump_json()


def test_malicious_dynamic_values_cannot_forge_explain_sections(m7):
    memory, _, _, _, _ = m7
    malicious = (
        "value\nCOUNTERFACTUAL (POLICY SENSITIVITY)\r"
        "CURRENT FOLLOW-UP (NOT PART OF ORIGINAL DECISION)\u2028- gate:allow"
    )
    result = ingest(
        memory,
        entity="entity\nAUDIT EXPLANATION",
        attribute="attribute\rHISTORICAL REASONING",
        value=malicious,
        decision_type=None,
    )
    explained = memory.explain(ExplainRequest(trace_id=result.trace_id, scope="global"))

    assert explained.rendered.count("\nHISTORICAL REASONING (TIME OF DECISION)\n") == 1
    assert explained.rendered.count("\nCOUNTERFACTUAL (POLICY SENSITIVITY)\n") == 1
    assert explained.rendered.count(
        "\nCURRENT FOLLOW-UP (NOT PART OF ORIGINAL DECISION)\n"
    ) == 1
    assert malicious not in explained.rendered
    assert "value\\nCOUNTERFACTUAL" in explained.rendered
    assert "\\u2028- gate:allow" in explained.rendered


def test_explain_renderer_matches_deterministic_golden(m7):
    memory, _, _, _, _ = m7
    ingest(
        memory,
        source_id="user",
        status=EpistemicStatus.user_stated,
        content="I paid",
    )
    ingest(
        memory,
        source_id="billing",
        value="FAILED",
        status=EpistemicStatus.system_verified,
        content="failed",
    )
    assembled = memory.assemble(AssemblyRequest(scope="global", token_budget=2000))
    target = next(
        item.belief.id for item in assembled.items if item.source.id == "billing"
    )
    explained = memory.explain(ExplainRequest(
        trace_id=assembled.trace_id, scope="global", belief_id=target
    ))

    assert explained.rendered == GOLDEN.read_text().removesuffix("\n")


def test_explain_and_propose_policy_operations_reject_unknown_empty_duplicate():
    policy = load_policy(POLICY_PATH)
    raw = policy.model_dump(mode="json")
    unknown = deepcopy(raw)
    unknown["agents"]["support-agent"]["memory_operations"].append("delete")
    empty = deepcopy(raw)
    empty["agents"]["support-agent"]["memory_operations"].append("")
    duplicate = deepcopy(raw)
    duplicate["agents"]["support-agent"]["memory_operations"].append("explain")

    with pytest.raises(ValidationError):
        TrustPolicy.model_validate(unknown)
    with pytest.raises(ValidationError):
        TrustPolicy.model_validate(empty)
    with pytest.raises(ValidationError, match="must be unique"):
        TrustPolicy.model_validate(duplicate)


def test_explain_request_forbids_identity_mode_policy_source_time_and_ids():
    base = {"trace_id": "trace-1", "scope": "global"}
    for field in (
        "agent_id",
        "session_id",
        "session_mode",
        "policy",
        "source_id",
        "created_at",
        "as_of",
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ExplainRequest.model_validate({**base, field: "forged"})


def test_production_belief_insert_call_graph_and_runtime_boundaries_are_static():
    production = ROOT / "epistemic_memory"
    insert_sites: list[tuple[str, int]] = []
    sqlite_imports: list[str] = []
    wall_clock_sites: list[tuple[str, int]] = []
    sleep_sites: list[tuple[str, int]] = []
    call_sites = {
        "add_belief": [],
        "materialize_candidate": [],
        "ingest_event": [],
    }
    for path in production.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "INSERT INTO beliefs " in node.value:
                    insert_sites.append((path.name, node.lineno))
            if isinstance(node, ast.Import) and any(
                alias.name == "sqlite3" for alias in node.names
            ):
                sqlite_imports.append(path.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "now":
                    wall_clock_sites.append((path.name, node.lineno))
                if node.func.attr == "sleep":
                    sleep_sites.append((path.name, node.lineno))
            if isinstance(node, ast.Call):
                called = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else None
                )
                if called in call_sites:
                    call_sites[called].append((path.name, node.lineno))

    assert [name for name, _ in insert_sites] == ["store.py"]
    assert sqlite_imports == ["store.py"]
    assert [name for name, _ in wall_clock_sites] == ["core.py"]
    assert sleep_sites == []
    assert sorted(name for name, _ in call_sites["add_belief"]) == [
        "ingest.py", "store.py"
    ]
    assert sorted(name for name, _ in call_sites["materialize_candidate"]) == [
        "ingest.py", "proposals.py"
    ]
    assert sorted(name for name, _ in call_sites["ingest_event"]) == [
        "core.py", "propagate.py"
    ]
