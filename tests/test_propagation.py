"""M6 verification: explicit dependency DAG and atomic correction propagation."""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    ArtifactExecutionState,
    ArtifactKind,
    ArtifactPropagationState,
    ArtifactRegistrationRequest,
    Belief,
    CandidateBelief,
    CommitmentCreateRequest,
    CommitmentPrecondition,
    CommitmentResultCode,
    CommitmentTransitionRequest,
    CorrectionKind,
    CorrectionRequest,
    DependencyEndpointKind,
    DependencyRegistrationRequest,
    EpistemicStatus,
    M6ResultCode,
    Source,
    TrustPolicy,
)
from epistemic_memory.policy import load_policy
from epistemic_memory.propagate import apply_propagation


POLICY_PATH = str(Path(__file__).resolve().parents[1] / "trust_policy.yaml")
NOW = datetime(2026, 7, 11, 9, 30, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, instant: datetime):
        self.instant = instant
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.instant

    def set(self, instant: datetime) -> None:
        self.instant = instant


@pytest.fixture
def m6(tmp_path):
    db_path = str(tmp_path / "m6.db")
    policy = load_policy(POLICY_PATH)
    clock = FakeClock(NOW)
    memory = MemoryStore(
        db_path, policy, agent_id="support-agent", clock=clock
    )
    for source_id, source_type in [
        ("user", "user"),
        ("billing", "billing_system"),
    ]:
        memory._store.add_source(Source(
            id=source_id,
            type=source_type,
            label=source_id,
            created_at=NOW.isoformat(),
        ))
    yield memory, policy, db_path, clock
    memory.close()


def belief(memory: MemoryStore, **overrides) -> Belief:
    values = {
        "entity": "order_4411",
        "attribute": "payment_status",
        "value": "paid",
        "status": EpistemicStatus.user_stated,
        "scope": "global",
        "source_id": "user",
        "decision_type": "payment_status",
        "valid_from": NOW.isoformat(),
        "created_at": NOW.isoformat(),
    }
    values.update(overrides)
    return memory._store.add_belief(Belief(**values))


def ingest_belief(memory: MemoryStore, **overrides) -> Belief:
    values = {
        "entity": "order_4411",
        "attribute": "payment_status",
        "value": "paid",
        "proposed_status": EpistemicStatus.user_stated,
        "scope": "global",
        "decision_type": "payment_status",
        "source_id": "user",
        "content": "I paid",
    }
    values.update(overrides)
    source_id = values.pop("source_id")
    content = values.pop("content")

    def extractor(event, source_type):
        return [CandidateBelief(**values)]

    return memory.ingest(
        source_id=source_id,
        content=content,
        scope=values["scope"],
        extractor=extractor,
    ).beliefs[0]


def artifact(
    memory: MemoryStore,
    *,
    label: str,
    kind: ArtifactKind = ArtifactKind.output,
    execution_state: ArtifactExecutionState = ArtifactExecutionState.not_applicable,
    scope: str | None = "global",
    reference: str | None = None,
):
    result = memory.register_artifact(ArtifactRegistrationRequest(
        kind=kind,
        execution_state=execution_state,
        scope=scope,
        label=label,
        reference=reference,
    ))
    assert result.ok, result
    return result.artifact


def dependency(
    memory: MemoryStore,
    upstream_kind: DependencyEndpointKind,
    upstream_id: int,
    downstream_artifact_id: int,
    *,
    scope: str | None = "global",
    task_type: str | None = None,
):
    return memory.register_dependency(DependencyRegistrationRequest(
        upstream_kind=upstream_kind,
        upstream_id=upstream_id,
        downstream_artifact_id=downstream_artifact_id,
        scope=scope,
        task_type=task_type,
    ))


def correction(
    memory: MemoryStore,
    belief_id: int,
    *,
    source_id: str = "user",
    scope: str | None = "global",
    value: str | None = "unpaid",
    proposed_status: EpistemicStatus | None = EpistemicStatus.user_stated,
    kind: CorrectionKind = CorrectionKind.correction,
):
    return memory.correct(CorrectionRequest(
        belief_id=belief_id,
        kind=kind,
        source_id=source_id,
        content="Correction received",
        scope=scope,
        value=value,
        proposed_status=proposed_status,
    ))


def test_register_artifacts_preserves_kind_execution_and_clock(m6):
    memory, _, _, clock = m6
    output = artifact(memory, label="Generated answer")
    pending = artifact(
        memory,
        label="Pending refund",
        kind=ArtifactKind.action,
        execution_state=ArtifactExecutionState.pending,
    )
    executed = artifact(
        memory,
        label="Sent message",
        kind=ArtifactKind.action,
        execution_state=ArtifactExecutionState.executed,
    )

    assert output.execution_state == ArtifactExecutionState.not_applicable
    assert pending.execution_state == ArtifactExecutionState.pending
    assert executed.execution_state == ArtifactExecutionState.executed
    assert all(
        item.propagation_state == ArtifactPropagationState.current
        for item in (output, pending, executed)
    )
    assert all(item.created_by_agent_id == "support-agent" for item in (output, pending, executed))
    assert all(item.created_at == item.updated_at == NOW for item in (output, pending, executed))
    assert clock.calls == 3


@pytest.mark.parametrize(
    ("kind", "execution_state"),
    [
        (ArtifactKind.output, ArtifactExecutionState.pending),
        (ArtifactKind.action, ArtifactExecutionState.not_applicable),
    ],
)
def test_artifact_kind_execution_combinations_are_validated(kind, execution_state):
    with pytest.raises(ValidationError):
        ArtifactRegistrationRequest(
            kind=kind,
            execution_state=execution_state,
            scope="global",
            label="invalid",
        )


def test_registers_belief_to_artifact_and_artifact_to_artifact_edges(m6):
    memory, _, _, _ = m6
    upstream_belief = belief(memory)
    answer = artifact(memory, label="Answer")
    action = artifact(
        memory,
        label="Action",
        kind=ArtifactKind.action,
        execution_state=ArtifactExecutionState.pending,
    )

    first = dependency(
        memory, DependencyEndpointKind.belief, upstream_belief.id, answer.id
    )
    second = dependency(
        memory, DependencyEndpointKind.artifact, answer.id, action.id
    )

    assert first.code == second.code == M6ResultCode.dependency_registered
    assert first.dependency.upstream_kind == DependencyEndpointKind.belief
    assert second.dependency.upstream_kind == DependencyEndpointKind.artifact
    assert first.dependency.created_at == second.dependency.created_at == NOW


def test_dangling_endpoints_and_self_edges_fail_closed(m6):
    memory, _, _, _ = m6
    answer = artifact(memory, label="Answer")

    missing_upstream = dependency(
        memory, DependencyEndpointKind.belief, 999_999, answer.id
    )
    missing_downstream = dependency(
        memory, DependencyEndpointKind.belief, belief(memory).id, 999_999
    )
    self_edge = dependency(
        memory, DependencyEndpointKind.artifact, answer.id, answer.id
    )

    assert missing_upstream.code == M6ResultCode.dependency_endpoint_not_found
    assert missing_downstream.code == M6ResultCode.artifact_not_found
    assert self_edge.code == M6ResultCode.dependency_self_edge
    assert memory._store.conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0


def test_duplicate_edge_is_deterministic_and_propagates_once(m6):
    memory, _, _, _ = m6
    upstream = belief(memory)
    answer = artifact(memory, label="Answer")

    first = dependency(memory, DependencyEndpointKind.belief, upstream.id, answer.id)
    duplicate = dependency(memory, DependencyEndpointKind.belief, upstream.id, answer.id)
    result = correction(memory, upstream.id)

    assert first.ok is True
    assert duplicate.ok is False
    assert duplicate.code == M6ResultCode.dependency_duplicate
    assert memory._store.conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 1
    assert result.affected_count == 1
    assert [impact.artifact.id for impact in result.visible_impacts] == [answer.id]


def test_cycle_registration_is_rejected(m6):
    memory, _, _, _ = m6
    first = artifact(memory, label="First")
    second = artifact(memory, label="Second")
    third = artifact(memory, label="Third")
    assert dependency(memory, DependencyEndpointKind.artifact, first.id, second.id).ok
    assert dependency(memory, DependencyEndpointKind.artifact, second.id, third.id).ok

    cycle = dependency(
        memory, DependencyEndpointKind.artifact, third.id, first.id
    )

    assert cycle.code == M6ResultCode.dependency_cycle
    assert memory._store.conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 2


def test_scope_incompatible_dependency_rejects_hobby_evidence_for_banking(m6):
    memory, _, _, _ = m6
    hobby = belief(
        memory,
        entity="user",
        attribute="style",
        value="pixel art",
        scope="project:hobby",
        decision_type=None,
    )
    banking = artifact(memory, label="Banking answer", scope="project:banking")

    result = dependency(
        memory,
        DependencyEndpointKind.belief,
        hobby.id,
        banking.id,
        scope="project:banking",
    )

    assert result.code == M6ResultCode.dependency_scope_incompatible
    assert memory._store.conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0


def test_global_upstream_may_support_narrower_artifact(m6):
    memory, _, _, _ = m6
    upstream = belief(memory, scope="global")
    banking = artifact(memory, label="Banking answer", scope="project:banking")
    result = dependency(
        memory,
        DependencyEndpointKind.belief,
        upstream.id,
        banking.id,
        scope="project:banking",
    )
    assert result.ok is True


def test_correction_appends_event_and_belief_without_mutating_history_and_clamps(m6):
    memory, _, _, clock = m6
    original = ingest_belief(memory)
    original_event = memory._store.conn.execute(
        "SELECT * FROM events WHERE id = ?", (original.event_id,)
    ).fetchone()
    original_belief = memory._store.conn.execute(
        "SELECT * FROM beliefs WHERE id = ?", (original.id,)
    ).fetchone()
    before_events = memory._store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_beliefs = memory._store.conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]
    calls_before = clock.calls

    result = correction(
        memory,
        original.id,
        value="refunded",
        proposed_status=EpistemicStatus.system_verified,
    )

    assert result.ok is True
    assert result.code == M6ResultCode.correction_applied
    assert result.belief.supersedes_id == original.id
    assert result.belief.status == EpistemicStatus.user_stated
    assert result.event.source_id == result.belief.source_id == original.source_id
    assert memory._store.is_current(original.id) is False
    assert memory._store.is_current(result.belief.id) is True
    assert memory._store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events + 1
    assert memory._store.conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == before_beliefs + 1
    assert tuple(memory._store.conn.execute(
        "SELECT * FROM events WHERE id = ?", (original.event_id,)
    ).fetchone()) == tuple(original_event)
    assert tuple(memory._store.conn.execute(
        "SELECT * FROM beliefs WHERE id = ?", (original.id,)
    ).fetchone()) == tuple(original_belief)
    assert result.event.created_at == result.belief.created_at == NOW.isoformat()
    assert clock.calls == calls_before + 1


def test_retraction_is_a_new_current_belief_not_deletion(m6):
    memory, _, _, _ = m6
    original = ingest_belief(memory)
    before = memory._store.conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]

    result = correction(
        memory,
        original.id,
        kind=CorrectionKind.retraction,
        value=None,
        proposed_status=None,
    )

    assert result.ok is True
    assert result.belief.status == EpistemicStatus.retracted
    assert result.belief.value == original.value
    assert result.belief.supersedes_id == original.id
    assert memory._store.conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == before + 1
    assert memory._store.get_belief(original.id) is not None


def test_cross_source_correction_is_denied_and_disagreement_uses_conflict_path(m6):
    memory, _, _, _ = m6
    original = ingest_belief(memory)

    denied = correction(memory, original.id, source_id="billing")
    billing = ingest_belief(
        memory,
        source_id="billing",
        content="billing reports failed",
        value="FAILED",
        proposed_status=EpistemicStatus.system_verified,
    )
    current = memory._store.current_beliefs(original.entity, original.attribute)

    assert denied.code == M6ResultCode.source_mismatch
    assert denied.belief is None
    assert {item.id for item in current} == {original.id, billing.id}
    assert {item.value for item in current} == {"paid", "FAILED"}
    assert all(item.supersedes_id is None for item in current)


def test_non_current_target_and_semantically_invalid_requests_fail_deterministically(m6):
    memory, _, _, _ = m6
    original = belief(memory)
    applied = correction(memory, original.id)
    repeated = correction(memory, original.id)
    missing_value = correction(
        memory,
        applied.belief.id,
        value=None,
        proposed_status=EpistemicStatus.user_stated,
    )
    invalid_retraction = correction(
        memory,
        applied.belief.id,
        kind=CorrectionKind.retraction,
        value="unexpected",
        proposed_status=None,
    )

    assert repeated.code == M6ResultCode.target_belief_not_current
    assert missing_value.code == M6ResultCode.invalid_correction
    assert invalid_retraction.code == M6ResultCode.invalid_correction


def test_missing_and_out_of_task_targets_fail_with_structured_codes(m6):
    memory, _, _, _ = m6
    hidden = belief(memory, scope="project:hobby")

    missing = correction(memory, 999_999)
    denied = correction(memory, hidden.id, scope="project:banking")

    assert missing.code == M6ResultCode.target_belief_not_found
    assert denied.code == M6ResultCode.scope_denied
    assert memory._store.is_current(hidden.id) is True


def test_direct_artifact_responses_and_unrelated_artifact_are_exact(m6):
    memory, _, _, _ = m6
    upstream = belief(memory)
    output = artifact(memory, label="Generated answer")
    pending = artifact(
        memory,
        label="Pending refund",
        kind=ArtifactKind.action,
        execution_state=ArtifactExecutionState.pending,
    )
    executed = artifact(
        memory,
        label="Already sent",
        kind=ArtifactKind.action,
        execution_state=ArtifactExecutionState.executed,
    )
    unrelated = artifact(memory, label="Unrelated")
    for item in (output, pending, executed):
        assert dependency(
            memory, DependencyEndpointKind.belief, upstream.id, item.id
        ).ok

    result = correction(memory, upstream.id)
    stored = {item.id: item for item in memory._store.list_artifacts()}
    impacts = {item.artifact.id: item for item in result.visible_impacts}

    assert stored[output.id].propagation_state == ArtifactPropagationState.stale
    assert stored[pending.id].propagation_state == ArtifactPropagationState.halted
    assert stored[executed.id].execution_state == ArtifactExecutionState.executed
    assert stored[executed.id].propagation_state == ArtifactPropagationState.review_required
    assert stored[unrelated.id].propagation_state == ArtifactPropagationState.current
    assert impacts[output.id].reason_code == M6ResultCode.artifact_marked_stale
    assert impacts[pending.id].reason_code == M6ResultCode.pending_action_halted
    assert impacts[executed.id].reason_code == M6ResultCode.executed_action_requires_review
    assert all(item.rule_id.startswith("M6-") for item in impacts.values())


def test_transitive_diamond_is_once_and_report_order_is_depth_then_id(m6, monkeypatch):
    memory, _, _, _ = m6
    upstream = belief(memory)
    first = artifact(memory, label="First")
    second = artifact(memory, label="Second")
    diamond = artifact(memory, label="Diamond")
    tail = artifact(memory, label="Tail")
    for item in (first, second):
        assert dependency(
            memory, DependencyEndpointKind.belief, upstream.id, item.id
        ).ok
        assert dependency(
            memory, DependencyEndpointKind.artifact, item.id, diamond.id
        ).ok
    assert dependency(
        memory, DependencyEndpointKind.artifact, diamond.id, tail.id
    ).ok
    original_downstream = memory._store.downstream_artifact_ids

    def reversed_downstream(kind, endpoint_id):
        return list(reversed(original_downstream(kind, endpoint_id)))

    monkeypatch.setattr(memory._store, "downstream_artifact_ids", reversed_downstream)
    result = correction(memory, upstream.id)

    assert [(impact.depth, impact.artifact.id) for impact in result.visible_impacts] == [
        (1, first.id),
        (1, second.id),
        (2, diamond.id),
        (3, tail.id),
    ]
    assert [impact.artifact.id for impact in result.visible_impacts].count(diamond.id) == 1
    assert result.affected_count == 4


def test_propagation_is_cycle_safe_for_malformed_legacy_graph_and_idempotent(m6):
    memory, _, _, _ = m6
    upstream = belief(memory)
    first = artifact(memory, label="First")
    second = artifact(memory, label="Second")
    memory._store.add_dependency(
        DependencyEndpointKind.belief,
        upstream.id,
        first.id,
        created_by_agent_id="legacy",
        created_at=NOW,
    )
    memory._store.add_dependency(
        DependencyEndpointKind.artifact,
        first.id,
        second.id,
        created_by_agent_id="legacy",
        created_at=NOW,
    )
    memory._store.add_dependency(
        DependencyEndpointKind.artifact,
        second.id,
        first.id,
        created_by_agent_id="legacy",
        created_at=NOW,
    )

    first_pass, hidden_first, count_first = apply_propagation(
        memory._store, upstream.id, visible_scopes=["global"], as_of=NOW
    )
    second_pass, hidden_second, count_second = apply_propagation(
        memory._store,
        upstream.id,
        visible_scopes=["global"],
        as_of=NOW + timedelta(hours=1),
    )

    assert [item.artifact.id for item in first_pass] == [first.id, second.id]
    assert [item.artifact.id for item in second_pass] == [first.id, second.id]
    assert all(item.state_changed is True for item in first_pass)
    assert all(item.state_changed is False for item in second_pass)
    assert hidden_first == hidden_second == 0
    assert count_first == count_second == 2


def test_one_changed_dependency_conservatively_invalidates_multi_supported_artifact(m6):
    memory, _, _, _ = m6
    changed = belief(memory, value="paid")
    still_current = belief(
        memory,
        source_id="billing",
        value="paid",
        status=EpistemicStatus.system_verified,
    )
    answer = artifact(memory, label="Doubly supported answer")
    assert dependency(memory, DependencyEndpointKind.belief, changed.id, answer.id).ok
    assert dependency(memory, DependencyEndpointKind.belief, still_current.id, answer.id).ok

    result = correction(memory, changed.id)

    assert result.affected_count == 1
    assert result.visible_impacts[0].artifact.propagation_state == ArtifactPropagationState.stale
    assert memory._store.is_current(still_current.id) is True


def test_hidden_scope_artifact_is_halted_without_serialized_nonaggregate_leak(m6):
    memory, _, _, _ = m6
    upstream = belief(memory, scope="global")
    visible = artifact(memory, label="Visible global output", scope="global")
    secret_label = "HIDDEN_HOBBY_ACTION_LABEL"
    secret_reference = "secret://hidden-action-991"
    hidden = artifact(
        memory,
        label=secret_label,
        reference=secret_reference,
        kind=ArtifactKind.action,
        execution_state=ArtifactExecutionState.pending,
        scope="project:hobby",
    )
    assert dependency(
        memory, DependencyEndpointKind.belief, upstream.id, visible.id
    ).ok
    assert dependency(
        memory,
        DependencyEndpointKind.artifact,
        visible.id,
        hidden.id,
        scope="project:hobby",
    ).ok

    result = memory.correct(CorrectionRequest(
        belief_id=upstream.id,
        kind=CorrectionKind.correction,
        source_id="user",
        content="global correction",
        scope="project:banking",
        value="unpaid",
        proposed_status=EpistemicStatus.user_stated,
    ))
    serialized = result.model_dump_json()

    assert memory._store.get_artifact(hidden.id).propagation_state == ArtifactPropagationState.halted
    assert [item.artifact.id for item in result.visible_impacts] == [visible.id]
    assert result.hidden_impacts[0].count == 1
    assert result.hidden_impacts[0].reason_code == M6ResultCode.hidden_downstream_impacts
    assert result.affected_count == 2
    assert secret_label not in serialized
    assert secret_reference not in serialized
    assert "project:hobby" not in serialized


def test_correction_and_all_propagation_roll_back_on_injected_store_failure(m6, monkeypatch):
    memory, _, _, _ = m6
    upstream = ingest_belief(memory)
    first = artifact(memory, label="First")
    second = artifact(memory, label="Second")
    for item in (first, second):
        assert dependency(
            memory, DependencyEndpointKind.belief, upstream.id, item.id
        ).ok
    before_events = memory._store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_beliefs = memory._store.conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]
    original_update = memory._store.set_artifact_propagation_state
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected persistence failure")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(memory._store, "set_artifact_propagation_state", fail_second)
    result = correction(memory, upstream.id)

    assert result.code == M6ResultCode.atomic_propagation_failure
    assert memory._store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
    assert memory._store.conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == before_beliefs
    assert memory._store.is_current(upstream.id) is True
    assert all(
        memory._store.get_artifact(item.id).propagation_state == ArtifactPropagationState.current
        for item in (first, second)
    )


def test_missing_scope_fails_closed_for_every_m6_public_operation(m6):
    memory, _, _, _ = m6
    upstream = belief(memory)
    answer = artifact(memory, label="Answer")

    create = memory.register_artifact(ArtifactRegistrationRequest(
        kind=ArtifactKind.output,
        execution_state=ArtifactExecutionState.not_applicable,
        scope=None,
        label="Missing scope",
    ))
    edge = dependency(
        memory,
        DependencyEndpointKind.belief,
        upstream.id,
        answer.id,
        scope=None,
    )
    corrected = correction(memory, upstream.id, scope=None)

    assert create.code == edge.code == corrected.code == M6ResultCode.scope_context_missing


@pytest.mark.parametrize("agent_id", ["analytics-bot", "unknown-agent"])
def test_unauthorized_agents_cannot_mutate_m6_state_without_content_leak(m6, agent_id):
    memory, policy, db_path, _ = m6
    secret = "M6_AUTHORITY_SECRET_LABEL"
    upstream = belief(memory)
    downstream = artifact(memory, label=secret)
    other = MemoryStore(db_path, policy, agent_id=agent_id, clock=FakeClock(NOW))
    try:
        create = other.register_artifact(ArtifactRegistrationRequest(
            kind=ArtifactKind.output,
            execution_state=ArtifactExecutionState.not_applicable,
            scope="global",
            label="denied",
        ))
        edge = other.register_dependency(DependencyRegistrationRequest(
            upstream_kind=DependencyEndpointKind.belief,
            upstream_id=upstream.id,
            downstream_artifact_id=downstream.id,
            scope="global",
        ))
        corrected = other.correct(CorrectionRequest(
            belief_id=upstream.id,
            kind=CorrectionKind.correction,
            source_id="user",
            content="denied correction",
            scope="global",
            value="unpaid",
            proposed_status=EpistemicStatus.user_stated,
        ))
    finally:
        other.close()

    expected = (
        M6ResultCode.operation_not_permitted
        if agent_id == "analytics-bot"
        else M6ResultCode.agent_unknown
    )
    assert create.code == edge.code == corrected.code == expected
    assert secret not in create.model_dump_json()
    assert secret not in edge.model_dump_json()
    assert secret not in corrected.model_dump_json()
    assert memory._store.is_current(upstream.id) is True


def test_policy_rejects_unknown_and_duplicate_memory_operations():
    policy = load_policy(POLICY_PATH)
    raw = policy.model_dump(mode="json")
    unknown = deepcopy(raw)
    unknown["agents"]["support-agent"]["memory_operations"].append("delete")
    duplicate = deepcopy(raw)
    duplicate["agents"]["support-agent"]["memory_operations"].append("correct")

    with pytest.raises(ValidationError):
        TrustPolicy.model_validate(unknown)
    with pytest.raises(ValidationError, match="must be unique"):
        TrustPolicy.model_validate(duplicate)


def test_public_m6_requests_forbid_agent_and_authoritative_timestamps():
    requests = [
        (
            ArtifactRegistrationRequest,
            {
                "kind": "output",
                "execution_state": "not_applicable",
                "scope": "global",
                "label": "answer",
            },
        ),
        (
            DependencyRegistrationRequest,
            {
                "upstream_kind": "belief",
                "upstream_id": 1,
                "downstream_artifact_id": 1,
                "scope": "global",
            },
        ),
        (
            CorrectionRequest,
            {
                "belief_id": 1,
                "kind": "correction",
                "source_id": "user",
                "content": "correct",
                "scope": "global",
                "value": "new",
                "proposed_status": "user_stated",
            },
        ),
    ]
    for model, values in requests:
        for field in ("agent_id", "created_at", "updated_at", "as_of"):
            with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
                model.model_validate({**values, field: NOW})


def test_one_clock_sample_drives_correction_event_belief_and_all_artifacts(m6):
    memory, _, _, clock = m6
    upstream = belief(memory)
    output = artifact(memory, label="Output")
    pending = artifact(
        memory,
        label="Pending",
        kind=ArtifactKind.action,
        execution_state=ArtifactExecutionState.pending,
    )
    for item in (output, pending):
        assert dependency(
            memory, DependencyEndpointKind.belief, upstream.id, item.id
        ).ok
    operation_time = NOW + timedelta(days=2)
    clock.set(operation_time)
    calls_before = clock.calls

    result = correction(memory, upstream.id)

    assert clock.calls == calls_before + 1
    assert result.as_of == operation_time
    assert result.event.created_at == operation_time.isoformat()
    assert result.belief.created_at == operation_time.isoformat()
    assert all(item.artifact.updated_at == operation_time for item in result.visible_impacts)


def test_correction_keeps_commitment_state_and_invalidates_future_fulfillment(m6):
    memory, _, _, clock = m6
    upstream = belief(memory)
    commitment = memory.add_commitment(CommitmentCreateRequest(
        description="Refund after payment confirmation",
        owner="refund-operations",
        beneficiary="customer_881",
        scope="global",
        deadline=NOW + timedelta(days=5),
        preconditions=[CommitmentPrecondition(belief_id=upstream.id)],
        proof_required=False,
    )).commitment
    correction(memory, upstream.id)
    clock.set(NOW + timedelta(hours=1))

    fulfillment = memory.transition_commitment(CommitmentTransitionRequest(
        commitment_id=commitment.id,
        target_state="fulfilled",
        scope="global",
    ))

    assert fulfillment.code == CommitmentResultCode.precondition_unsatisfied
    assert memory._store.get_commitment(commitment.id).state.value == "open"


def test_artifact_definition_is_immutable_and_delete_is_blocked(m6):
    memory, _, _, _ = m6
    answer = artifact(memory, label="Stable identity")

    with pytest.raises(sqlite3.IntegrityError, match="definition is immutable"):
        memory._store.conn.execute(
            "UPDATE artifacts SET execution_state = 'executed' WHERE id = ?",
            (answer.id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        memory._store.conn.execute(
            "DELETE FROM artifacts WHERE id = ?", (answer.id,)
        )


def test_empty_pre_m6_artifact_placeholders_upgrade_without_touching_beliefs(tmp_path):
    db_path = str(tmp_path / "m5-schema.db")
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE dependencies (
            id INTEGER PRIMARY KEY,
            artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
            belief_id INTEGER NOT NULL
        );
        """
    )
    legacy.close()

    memory = MemoryStore(
        db_path,
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        clock=FakeClock(NOW),
    )
    try:
        artifact_columns = {
            row[1]
            for row in memory._store.conn.execute("PRAGMA table_info(artifacts)")
        }
        dependency_columns = {
            row[1]
            for row in memory._store.conn.execute("PRAGMA table_info(dependencies)")
        }
    finally:
        memory.close()

    assert {"execution_state", "propagation_state", "scope"}.issubset(artifact_columns)
    assert {"upstream_belief_id", "upstream_artifact_id"}.issubset(dependency_columns)


def test_pre_m6_out_of_band_artifact_rows_fail_closed_without_deletion(tmp_path):
    db_path = str(tmp_path / "m5-data.db")
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE dependencies (
            id INTEGER PRIMARY KEY,
            artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
            belief_id INTEGER NOT NULL
        );
        INSERT INTO artifacts VALUES (41, 'summary', 'legacy-ref', 'active', '2026-07-01');
        """
    )
    legacy.close()

    with pytest.raises(RuntimeError, match="lack required scope and execution provenance"):
        MemoryStore(
            db_path,
            load_policy(POLICY_PATH),
            agent_id="support-agent",
            clock=FakeClock(NOW),
        )

    check = sqlite3.connect(db_path)
    try:
        rows = check.execute("SELECT * FROM artifacts").fetchall()
    finally:
        check.close()
    assert rows == [(41, "summary", "legacy-ref", "active", "2026-07-01")]
