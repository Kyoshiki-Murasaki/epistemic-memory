"""M5 verification: first-class commitments, authority, lifecycle, and time."""

import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    AgentPermissions,
    Belief,
    CommitmentCreateRequest,
    CommitmentListRequest,
    CommitmentOperation,
    CommitmentPrecondition,
    CommitmentResultCode,
    CommitmentState,
    CommitmentTransitionRequest,
    EpistemicStatus,
    OverdueScanRequest,
    RiskTier,
    Source,
    TrustPolicy,
)
from epistemic_memory.policy import load_policy

POLICY_PATH = str(
    Path(__file__).resolve().parents[1]
    / "epistemic_memory"
    / "trust_policy.yaml"
)
CREATED = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
DEADLINE = CREATED + timedelta(days=5)


class FakeClock:
    def __init__(self, instant: datetime):
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant

    def set(self, instant: datetime) -> None:
        self.instant = instant


def set_time(memory: MemoryStore, instant: datetime) -> None:
    assert isinstance(memory._clock, FakeClock)
    memory._clock.set(instant)


@pytest.fixture
def commitments(tmp_path):
    db_path = str(tmp_path / "commitments.db")
    policy = load_policy(POLICY_PATH)
    memory = MemoryStore(
        db_path, policy, agent_id="support-agent", clock=FakeClock(CREATED)
    )
    memory._store.add_source(Source(
        id="user",
        type="user",
        label="Customer chat",
        created_at=CREATED.isoformat(),
    ))
    memory._store.add_source(Source(
        id="billing",
        type="billing_system",
        label="Billing system",
        created_at=CREATED.isoformat(),
    ))
    yield memory, policy, db_path
    memory.close()


def create_request(**overrides) -> CommitmentCreateRequest:
    values = {
        "description": "Refund the customer within 5 days.",
        "owner": "refund-operations",
        "beneficiary": "customer_881",
        "scope": "project:support",
        "deadline": DEADLINE,
        "preconditions": [],
        "proof_required": False,
    }
    values.update(overrides)
    return CommitmentCreateRequest(**values)


def add(memory: MemoryStore, **overrides):
    result = memory.add_commitment(create_request(**overrides))
    assert result.ok is True
    assert result.code == CommitmentResultCode.commitment_created
    return result.commitment


def transition(
    memory: MemoryStore,
    commitment_id: int,
    target_state: str,
    *,
    scope: str | None = "project:support",
    at: datetime = CREATED + timedelta(hours=1),
    proof_reference: str | None = None,
):
    set_time(memory, at)
    return memory.transition_commitment(CommitmentTransitionRequest(
        commitment_id=commitment_id,
        target_state=target_state,
        scope=scope,
        proof_reference=proof_reference,
    ))


def scan(
    memory: MemoryStore,
    *,
    scope: str | None,
    at: datetime,
    task_type: str | None = None,
):
    set_time(memory, at)
    return memory.surface_overdue(OverdueScanRequest(
        scope=scope,
        task_type=task_type,
    ))


def add_belief(memory: MemoryStore, **overrides):
    values = {
        "entity": "order_4411",
        "attribute": "payment_status",
        "value": "paid",
        "status": EpistemicStatus.system_verified,
        "scope": "project:support",
        "source_id": "billing",
        "decision_type": "payment_status",
        "valid_from": CREATED.isoformat(),
        "created_at": CREATED.isoformat(),
    }
    values.update(overrides)
    return memory._store.add_belief(Belief(**values))


def test_create_first_class_commitment_with_distinct_owner_and_creator(commitments):
    memory, _, _ = commitments
    evidence = add_belief(memory)

    commitment = add(
        memory,
        owner="refund-operations",
        preconditions=[CommitmentPrecondition(
            belief_id=evidence.id, require_uncontradicted=True
        )],
        proof_required=True,
    )

    assert commitment.id > 0
    assert commitment.description == "Refund the customer within 5 days."
    assert commitment.owner == "refund-operations"
    assert commitment.owner != commitment.created_by_agent_id
    assert commitment.created_by_agent_id == "support-agent"
    assert commitment.beneficiary == "customer_881"
    assert commitment.scope == "project:support"
    assert commitment.state == CommitmentState.open
    assert commitment.deadline == DEADLINE
    assert commitment.preconditions[0].belief_id == evidence.id
    assert commitment.proof_required is True


def test_commitments_are_stored_separately_from_beliefs(commitments):
    memory, _, _ = commitments
    belief = add_belief(memory)
    commitment = add(memory)

    belief_rows = memory._store.conn.execute(
        "SELECT id FROM beliefs ORDER BY id"
    ).fetchall()
    commitment_rows = memory._store.conn.execute(
        "SELECT id FROM commitments ORDER BY id"
    ).fetchall()

    assert [row[0] for row in belief_rows] == [belief.id]
    assert [row[0] for row in commitment_rows] == [commitment.id]


@pytest.mark.parametrize(
    "target",
    ["waiting", "fulfilled", "cancelled"],
)
def test_every_open_transition_succeeds(commitments, target):
    memory, _, _ = commitments
    commitment = add(memory)
    result = transition(memory, commitment.id, target)

    assert result.ok is True
    assert result.code == CommitmentResultCode.commitment_transitioned
    assert result.commitment.state == CommitmentState(target)


@pytest.mark.parametrize("target", ["fulfilled", "cancelled"])
def test_waiting_reaches_every_terminal_state(commitments, target):
    memory, _, _ = commitments
    commitment = add(memory)
    waiting = transition(memory, commitment.id, "waiting")
    assert waiting.ok is True
    result = transition(
        memory, commitment.id, target, at=CREATED + timedelta(hours=2)
    )

    assert result.ok is True
    assert result.commitment.state == CommitmentState(target)


@pytest.mark.parametrize("terminal", ["fulfilled", "cancelled", "overdue"])
def test_terminal_states_reject_all_outbound_transitions(commitments, terminal):
    memory, _, _ = commitments
    commitment = add(memory)
    if terminal == "overdue":
        scanned = scan(
            memory,
            scope="project:support",
            at=DEADLINE + timedelta(seconds=1),
        )
        assert [item.id for item in scanned.overdue] == [commitment.id]
    else:
        assert transition(memory, commitment.id, terminal).ok

    result = transition(
        memory,
        commitment.id,
        "waiting",
        at=DEADLINE + timedelta(days=1),
    )

    assert result.ok is False
    assert result.code == CommitmentResultCode.transition_invalid
    assert result.commitment is None


def test_self_backward_unknown_state_and_unknown_id_are_structured(commitments):
    memory, _, _ = commitments
    commitment = add(memory)
    assert transition(memory, commitment.id, "waiting").ok

    self_transition = transition(memory, commitment.id, "waiting")
    backward = transition(memory, commitment.id, "open")
    unknown_state = transition(memory, commitment.id, "paused")
    unknown_id = transition(memory, 999_999, "waiting")

    assert self_transition.code == CommitmentResultCode.transition_invalid
    assert backward.code == CommitmentResultCode.transition_invalid
    assert unknown_state.code == CommitmentResultCode.state_unknown
    assert unknown_id.code == CommitmentResultCode.commitment_not_found


def test_manual_overdue_selection_is_rejected_in_favor_of_full_scan(commitments):
    memory, _, _ = commitments
    commitment = add(memory)

    manual = transition(
        memory,
        commitment.id,
        "overdue",
        at=DEADLINE + timedelta(seconds=1),
    )
    overdue_scan = scan(
        memory,
        scope="project:support",
        at=DEADLINE + timedelta(seconds=1),
    )

    assert manual.code == CommitmentResultCode.overdue_scan_required
    assert [item.id for item in overdue_scan.overdue] == [commitment.id]


def test_manual_transition_timestamp_cannot_move_backward(commitments):
    memory, _, _ = commitments
    commitment = add(memory)
    result = transition(
        memory,
        commitment.id,
        "waiting",
        at=CREATED - timedelta(seconds=1),
    )
    assert result.code == CommitmentResultCode.transition_invalid


def test_direct_creation_in_terminal_state_is_not_part_of_typed_api():
    raw = create_request().model_dump()
    raw["state"] = "fulfilled"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CommitmentCreateRequest.model_validate(raw)
    raw.pop("state")
    raw["created_by_agent_id"] = "analytics-bot"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CommitmentCreateRequest.model_validate(raw)


def test_public_requests_forbid_all_lifecycle_timestamp_inputs():
    creation = create_request().model_dump()
    transition_request = {
        "commitment_id": 1,
        "target_state": "waiting",
        "scope": "project:support",
    }
    scan_request = {"scope": "project:support"}

    for field in ("created_at", "updated_at"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            CommitmentCreateRequest.model_validate({**creation, field: CREATED})
    for field in ("as_of", "updated_at"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            CommitmentTransitionRequest.model_validate(
                {**transition_request, field: CREATED}
            )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OverdueScanRequest.model_validate({**scan_request, "as_of": CREATED})


def test_caller_cannot_backdate_creation_and_store_uses_clock(commitments):
    memory, _, _ = commitments
    raw = create_request().model_dump()
    raw["created_at"] = CREATED - timedelta(days=30)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CommitmentCreateRequest.model_validate(raw)

    commitment = add(memory)
    assert commitment.created_at == CREATED
    assert commitment.updated_at == CREATED


def test_fulfillment_enforces_present_current_usable_precondition(commitments):
    memory, _, _ = commitments
    evidence = add_belief(memory)
    commitment = add(
        memory,
        preconditions=[CommitmentPrecondition(belief_id=evidence.id)],
    )
    memory._store.supersede(
        evidence.id,
        Belief(
            entity=evidence.entity,
            attribute=evidence.attribute,
            value="FAILED",
            status=EpistemicStatus.system_verified,
            scope=evidence.scope,
            source_id=evidence.source_id,
            decision_type=evidence.decision_type,
            valid_from=(CREATED + timedelta(hours=1)).isoformat(),
            created_at=(CREATED + timedelta(hours=1)).isoformat(),
        ),
    )

    result = transition(memory, commitment.id, "fulfilled")

    assert result.ok is False
    assert result.code == CommitmentResultCode.precondition_unsatisfied
    assert memory._store.get_commitment(commitment.id).state == CommitmentState.open


def test_uncontradicted_precondition_reuses_structural_conflict_closure(commitments):
    memory, _, _ = commitments
    user_claim = add_belief(
        memory,
        value="paid",
        status=EpistemicStatus.user_stated,
        source_id="user",
    )
    add_belief(memory, value="FAILED")
    commitment = add(
        memory,
        preconditions=[CommitmentPrecondition(
            belief_id=user_claim.id, require_uncontradicted=True
        )],
    )

    result = transition(memory, commitment.id, "fulfilled")

    assert result.code == CommitmentResultCode.precondition_unsatisfied


def test_required_proof_is_enforced_and_retained_after_fulfillment(commitments):
    memory, _, _ = commitments
    evidence = add_belief(memory)
    commitment = add(
        memory,
        preconditions=[CommitmentPrecondition(belief_id=evidence.id)],
        proof_required=True,
    )

    missing = transition(memory, commitment.id, "fulfilled")
    fulfilled = transition(
        memory,
        commitment.id,
        "fulfilled",
        proof_reference="ticket:refund-4411",
    )

    assert missing.code == CommitmentResultCode.proof_required
    assert fulfilled.ok is True
    assert fulfilled.commitment.proof_reference == "ticket:refund-4411"
    reloaded = memory.list_commitments(CommitmentListRequest(scope="project:support"))
    assert reloaded.commitments[0].proof_reference == "ticket:refund-4411"


@pytest.mark.parametrize(
    "proof",
    ["", " padded ", "bad\nproof", "bad\x01proof", "x" * 2049],
)
def test_malformed_proof_references_fail_with_structured_code(commitments, proof):
    memory, _, _ = commitments
    commitment = add(memory, proof_required=True)

    result = transition(
        memory, commitment.id, "fulfilled", proof_reference=proof
    )

    assert result.ok is False
    assert result.code == CommitmentResultCode.proof_reference_invalid


def test_proof_is_rejected_for_non_fulfillment_transition(commitments):
    memory, _, _ = commitments
    commitment = add(memory)
    result = transition(
        memory, commitment.id, "waiting", proof_reference="ticket:4411"
    )
    assert result.code == CommitmentResultCode.proof_not_applicable


def test_future_and_exact_deadline_are_not_overdue(commitments):
    memory, _, _ = commitments
    commitment = add(memory)

    future = scan(
        memory, scope="project:support", at=DEADLINE - timedelta(seconds=1)
    )
    equality = scan(memory, scope="project:support", at=DEADLINE)
    next_instant = scan(
        memory,
        scope="project:support",
        at=DEADLINE + timedelta(microseconds=1),
    )

    assert future.overdue == []
    assert equality.overdue == []
    assert [item.id for item in next_instant.overdue] == [commitment.id]
    assert memory._store.get_commitment(commitment.id).state == CommitmentState.overdue


def test_caller_cannot_force_or_postpone_overdue_promotion(commitments):
    memory, _, _ = commitments
    commitment = add(memory)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OverdueScanRequest.model_validate({
            "scope": "project:support",
            "as_of": DEADLINE + timedelta(days=30),
        })
    premature = scan(
        memory, scope="project:support", at=DEADLINE - timedelta(seconds=1)
    )
    assert premature.overdue == []

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OverdueScanRequest.model_validate({
            "scope": "project:support",
            "as_of": DEADLINE - timedelta(days=30),
        })
    authoritative = scan(
        memory, scope="project:support", at=DEADLINE + timedelta(seconds=1)
    )
    assert [item.id for item in authoritative.overdue] == [commitment.id]


def test_overdue_scan_fails_closed_if_clock_moves_behind_updated_at(commitments):
    memory, _, _ = commitments
    commitment = add(memory)
    assert transition(
        memory,
        commitment.id,
        "waiting",
        at=DEADLINE + timedelta(days=2),
    ).ok

    with pytest.raises(ValueError, match="must not move backward"):
        scan(
            memory,
            scope="project:support",
            at=DEADLINE + timedelta(days=1),
        )


def test_open_and_waiting_promote_after_deadline_and_scan_is_idempotent(commitments):
    memory, _, _ = commitments
    open_commitment = add(memory)
    waiting_commitment = add(memory, description="Waiting refund")
    assert transition(memory, waiting_commitment.id, "waiting").ok
    authoritative_time = DEADLINE + timedelta(microseconds=1)

    first = scan(
        memory, scope="project:support", at=authoritative_time
    )
    second = scan(
        memory, scope="project:support", at=authoritative_time
    )

    assert first.promoted_count == 2
    assert second.promoted_count == 0
    assert [item.id for item in first.overdue] == [open_commitment.id, waiting_commitment.id]
    assert [item.id for item in second.overdue] == [open_commitment.id, waiting_commitment.id]
    assert [item.model_dump() for item in second.overdue] == [
        item.model_dump() for item in first.overdue
    ]
    assert all(item.state == CommitmentState.overdue for item in second.overdue)


def test_fulfilled_and_cancelled_never_surface_as_overdue(commitments):
    memory, _, _ = commitments
    fulfilled = add(memory)
    cancelled = add(memory, description="Cancelled refund")
    assert transition(memory, fulfilled.id, "fulfilled").ok
    assert transition(memory, cancelled.id, "cancelled").ok

    overdue_scan = scan(
        memory,
        scope="project:support",
        at=DEADLINE + timedelta(days=1),
    )

    assert overdue_scan.overdue == []
    assert memory._store.get_commitment(fulfilled.id).state == CommitmentState.fulfilled
    assert memory._store.get_commitment(cancelled.id).state == CommitmentState.cancelled


def test_overdue_order_is_deadline_then_creation_then_immutable_id(commitments):
    memory, _, _ = commitments
    later_deadline = add(
        memory,
        description="Later deadline",
        deadline=DEADLINE + timedelta(hours=1),
    )
    first_tie = add(memory, description="First tie")
    second_tie = add(memory, description="Second tie")

    overdue_scan = scan(
        memory,
        scope="project:support",
        at=DEADLINE + timedelta(days=1),
    )

    assert [item.id for item in overdue_scan.overdue] == [
        first_tie.id,
        second_tie.id,
        later_deadline.id,
    ]


def test_task_scope_listing_excludes_secret_from_complete_serialization(commitments):
    memory, _, _ = commitments
    visible = add(memory, scope="project:banking", description="Visible banking work")
    secret = "HOBBY_COMMITMENT_SECRET_MARKER"
    add(memory, scope="project:hobby", description=secret, owner=secret, beneficiary=secret)

    result = memory.list_commitments(CommitmentListRequest(scope="project:banking"))
    serialized = result.model_dump_json()

    assert [item.id for item in result.commitments] == [visible.id]
    assert secret not in serialized
    assert "project:hobby" not in serialized
    assert next(
        item.count for item in result.exclusions if item.rule_id == "C-SCOPE-TASK-001"
    ) == 1


def test_agent_scope_intersection_excludes_readable_task_secret(commitments):
    memory, policy, db_path = commitments
    global_commitment = add(memory, scope="global", description="Global work")
    secret = "AGENT_SCOPE_COMMITMENT_SECRET"
    add(memory, scope="project:banking", description=secret)
    analytics = MemoryStore(
        db_path, policy, agent_id="analytics-bot", clock=FakeClock(CREATED)
    )
    try:
        result = analytics.list_commitments(
            CommitmentListRequest(scope="project:banking")
        )
    finally:
        analytics.close()

    assert [item.id for item in result.commitments] == [global_commitment.id]
    assert secret not in result.model_dump_json()


def test_missing_scope_context_fails_closed_for_all_public_commitment_paths(commitments):
    memory, _, _ = commitments
    create = memory.add_commitment(create_request(scope=None))
    commitment = add(memory)
    manual = transition(memory, commitment.id, "waiting", scope=None)
    listing = memory.list_commitments(CommitmentListRequest(scope=None))
    overdue_scan = scan(memory, scope=None, at=DEADLINE + timedelta(days=1))

    assert create.code == CommitmentResultCode.scope_context_missing
    assert manual.code == CommitmentResultCode.scope_context_missing
    assert listing.code == CommitmentResultCode.scope_context_missing
    assert listing.commitments == []
    assert overdue_scan.code == CommitmentResultCode.scope_context_missing
    assert overdue_scan.overdue == []


def test_non_managing_known_agent_cannot_transition_scope_readable_commitment(commitments):
    memory, policy, db_path = commitments
    commitment = add(memory, scope="global")
    peer = AgentPermissions(
        max_action_tier=RiskTier.irreversible,
        allowed_scopes=["global"],
        commitment_operations=[CommitmentOperation.transition],
    )
    peer_policy = policy.model_copy(update={
        "agents": {**policy.agents, "peer-agent": peer}
    })
    other = MemoryStore(
        db_path, peer_policy, agent_id="peer-agent", clock=FakeClock(CREATED)
    )
    try:
        result = transition(other, commitment.id, "waiting", scope="global")
    finally:
        other.close()

    assert result.code == CommitmentResultCode.managing_agent_required
    assert result.commitment is None
    assert memory._store.get_commitment(commitment.id).state == CommitmentState.open


def test_analytics_bot_cannot_create_transition_or_trigger_overdue_mutation(commitments):
    memory, policy, db_path = commitments
    commitment = add(memory, scope="global")
    analytics = MemoryStore(
        db_path, policy, agent_id="analytics-bot", clock=FakeClock(CREATED)
    )
    try:
        create = analytics.add_commitment(create_request(scope="global"))
        manual = transition(
            analytics, commitment.id, "waiting", scope="global"
        )
        overdue_scan = scan(
            analytics, scope="global", at=DEADLINE + timedelta(days=1)
        )
    finally:
        analytics.close()

    assert create.code == CommitmentResultCode.operation_not_permitted
    assert manual.code == CommitmentResultCode.operation_not_permitted
    assert overdue_scan.code == CommitmentResultCode.operation_not_permitted
    assert overdue_scan.overdue == []
    assert memory._store.get_commitment(commitment.id).state == CommitmentState.open


def test_unknown_agent_is_denied_without_commitment_content_leak(commitments):
    memory, policy, db_path = commitments
    secret = "UNKNOWN_AGENT_COMMITMENT_SECRET"
    add(memory, scope="global", description=secret)
    ghost = MemoryStore(
        db_path, policy, agent_id="ghost", clock=FakeClock(CREATED)
    )
    try:
        listing = ghost.list_commitments(CommitmentListRequest(scope="global"))
        create = ghost.add_commitment(create_request(scope="global"))
    finally:
        ghost.close()

    assert listing.code == CommitmentResultCode.agent_unknown
    assert listing.commitments == []
    assert secret not in listing.model_dump_json()
    assert create.code == CommitmentResultCode.agent_unknown


def test_scope_denial_blocks_creation_and_manual_transition(commitments):
    memory, policy, db_path = commitments
    global_commitment = add(memory, scope="global")
    restricted = policy.model_copy(update={
        "agents": {
            **policy.agents,
            "restricted": AgentPermissions(
                max_action_tier=RiskTier.irreversible,
                allowed_scopes=["global"],
                commitment_operations=[
                    CommitmentOperation.create,
                    CommitmentOperation.transition,
                ],
            ),
        }
    })
    agent = MemoryStore(
        db_path, restricted, agent_id="restricted", clock=FakeClock(CREATED)
    )
    try:
        create = agent.add_commitment(create_request(scope="project:banking"))
        # It can see the global commitment but is not its managing principal.
        manual = transition(agent, global_commitment.id, "waiting", scope="global")
    finally:
        agent.close()

    assert create.code == CommitmentResultCode.scope_denied
    assert manual.code == CommitmentResultCode.managing_agent_required


def test_managing_agent_transition_scope_denial_does_not_leak_content(commitments):
    memory, _, _ = commitments
    secret = "TRANSITION_SCOPE_SECRET_MARKER"
    commitment = add(
        memory,
        scope="project:hobby",
        description=secret,
        owner=secret,
        beneficiary=secret,
    )

    result = transition(
        memory, commitment.id, "waiting", scope="project:banking"
    )

    assert result.code == CommitmentResultCode.scope_denied
    assert result.commitment is None
    assert secret not in result.model_dump_json()


def test_scan_is_scope_safe_and_does_not_mutate_unrelated_commitments(commitments):
    memory, _, _ = commitments
    banking = add(memory, scope="project:banking", description="Banking overdue")
    hobby = add(memory, scope="project:hobby", description="HOBBY_SCAN_SECRET")

    result = scan(
        memory,
        scope="project:banking",
        at=DEADLINE + timedelta(days=1),
    )

    assert [item.id for item in result.overdue] == [banking.id]
    assert memory._store.get_commitment(banking.id).state == CommitmentState.overdue
    assert memory._store.get_commitment(hobby.id).state == CommitmentState.open
    assert "HOBBY_SCAN_SECRET" not in result.model_dump_json()


def test_refund_within_five_days_scenario_surfaces_overdue(commitments):
    memory, _, _ = commitments
    refund = add(
        memory,
        description="Refund the customer within 5 days.",
        owner="refund-operations",
        beneficiary="customer_881",
        deadline=CREATED + timedelta(days=5),
    )

    overdue_scan = scan(
        memory,
        scope="project:support",
        at=CREATED + timedelta(days=5, microseconds=1),
    )

    assert [item.id for item in overdue_scan.overdue] == [refund.id]
    assert overdue_scan.overdue[0].description == "Refund the customer within 5 days."


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", ""),
        ("owner", "   "),
        ("beneficiary", ""),
        ("scope", "project:"),
        ("scope", "project:bad scope"),
        ("description", "x" * 4097),
        ("owner", "x" * 257),
        ("beneficiary", "x" * 257),
    ],
)
def test_empty_and_malformed_commitment_fields_are_rejected(field, value):
    raw = create_request().model_dump()
    raw[field] = value
    with pytest.raises(ValidationError):
        CommitmentCreateRequest.model_validate(raw)


def test_naive_deadline_order_and_malformed_preconditions_are_rejected(commitments):
    memory, _, _ = commitments
    with pytest.raises(ValidationError, match="timezone-aware"):
        create_request(deadline=DEADLINE.replace(tzinfo=None))
    past_deadline = memory.add_commitment(
        create_request(deadline=CREATED - timedelta(seconds=1))
    )
    assert past_deadline.code == CommitmentResultCode.deadline_invalid
    with pytest.raises(ValidationError, match="must be unique"):
        create_request(preconditions=[
            CommitmentPrecondition(belief_id=1),
            CommitmentPrecondition(belief_id=1),
        ])
    with pytest.raises(ValidationError):
        CommitmentPrecondition.model_validate({"belief_id": 0})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CommitmentPrecondition.model_validate({"belief_id": 1, "script": "run()"})


def test_default_runtime_clock_returns_aware_utc(tmp_path):
    memory = MemoryStore(
        str(tmp_path / "runtime-clock.db"),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
    )
    try:
        result = memory.add_commitment(create_request(
            scope="global",
            deadline=datetime(2099, 1, 1, tzinfo=timezone.utc),
        ))
    finally:
        memory.close()

    assert result.ok is True
    assert result.commitment.created_at.tzinfo is not None
    assert result.commitment.created_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "bad_instant",
    [
        CREATED.replace(tzinfo=None),
        CREATED.astimezone(timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_naive_or_non_utc_clock_result_fails_closed(tmp_path, bad_instant):
    memory = MemoryStore(
        str(tmp_path / "bad-clock.db"),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        clock=FakeClock(bad_instant),
    )
    try:
        with pytest.raises(ValueError, match="timezone-aware UTC"):
            memory.add_commitment(create_request(scope="global"))
    finally:
        memory.close()


def test_commitment_operation_policy_rejects_unknown_and_duplicate_entries():
    policy = load_policy(POLICY_PATH)
    raw = policy.model_dump(mode="json")
    unknown = deepcopy(raw)
    unknown["agents"]["support-agent"]["commitment_operations"].append("delete")
    duplicate = deepcopy(raw)
    duplicate["agents"]["support-agent"]["commitment_operations"].append("create")

    with pytest.raises(ValidationError):
        TrustPolicy.model_validate(unknown)
    with pytest.raises(ValidationError, match="must be unique"):
        TrustPolicy.model_validate(duplicate)


def test_commitment_definition_and_creator_are_immutable_and_delete_is_blocked(commitments):
    memory, _, _ = commitments
    commitment = add(memory)

    with pytest.raises(sqlite3.IntegrityError, match="definition is immutable"):
        memory._store.conn.execute(
            "UPDATE commitments SET created_by_agent_id = ? WHERE id = ?",
            ("analytics-bot", commitment.id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="definition is immutable"):
        memory._store.conn.execute(
            "UPDATE commitments SET id = ? WHERE id = ?",
            (commitment.id + 1000, commitment.id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cancelled, never deleted"):
        memory._store.conn.execute(
            "DELETE FROM commitments WHERE id = ?", (commitment.id,)
        )
