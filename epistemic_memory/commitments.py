"""First-class commitment lifecycle, scope, and authorization for M5.

This module contains the single transition table. It has no clock of its own:
creation, manual transitions, and overdue scans all receive explicit aware UTC
timestamps through typed requests.
"""

from __future__ import annotations

from .models import (
    Commitment,
    CommitmentCreateRequest,
    CommitmentExclusionSummary,
    CommitmentListRequest,
    CommitmentListResult,
    CommitmentMutationResult,
    CommitmentOperation,
    CommitmentResultCode,
    CommitmentState,
    CommitmentTransitionRequest,
    OverdueScanRequest,
    OverdueScanResult,
    RetrievalRequest,
    TrustPolicy,
)
from .retrieve import active_task_scopes, authorized_task_scopes, retrieve_beliefs
from .store import Store


ALLOWED_TRANSITIONS: dict[CommitmentState, frozenset[CommitmentState]] = {
    CommitmentState.open: frozenset({
        CommitmentState.waiting,
        CommitmentState.fulfilled,
        CommitmentState.cancelled,
        CommitmentState.overdue,
    }),
    CommitmentState.waiting: frozenset({
        CommitmentState.fulfilled,
        CommitmentState.cancelled,
        CommitmentState.overdue,
    }),
    CommitmentState.fulfilled: frozenset(),
    CommitmentState.cancelled: frozenset(),
    CommitmentState.overdue: frozenset(),
}


def _mutation_failure(
    code: CommitmentResultCode, message: str
) -> CommitmentMutationResult:
    return CommitmentMutationResult(ok=False, code=code, message=message)


def _agent_for_operation(
    policy: TrustPolicy, agent_id: str, operation: CommitmentOperation
):
    agent = policy.agents.get(agent_id)
    if agent is None:
        return None, CommitmentResultCode.agent_unknown
    if operation not in agent.commitment_operations:
        return None, CommitmentResultCode.operation_not_permitted
    return agent, None


def _scope_sets(scope: str, task_type: str | None, allowed_scopes: list[str]):
    active = active_task_scopes(scope, task_type)
    effective = authorized_task_scopes(scope, task_type, allowed_scopes)
    return active, effective


def _scope_exclusions(
    store: Store, active: list[str], effective: list[str]
) -> list[CommitmentExclusionSummary]:
    denied = [scope for scope in active if scope not in effective]
    return [
        CommitmentExclusionSummary(
            reason_code="task_scope_mismatch",
            rule_id="C-SCOPE-TASK-001",
            count=store.count_commitments(scopes=active, invert_scopes=True),
            safe_detail="commitment outside active task scopes",
        ),
        CommitmentExclusionSummary(
            reason_code="agent_scope_denied",
            rule_id="C-SCOPE-AGENT-001",
            count=store.count_commitments(scopes=denied),
            safe_detail="commitment outside agent readable scopes",
        ),
    ]


def _transition_allowed(current: CommitmentState, target: CommitmentState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def _proof_problem(reference: str | None) -> CommitmentResultCode | None:
    if reference is None:
        return None
    if not reference or reference != reference.strip() or len(reference) > 2048:
        return CommitmentResultCode.proof_reference_invalid
    if any(not character.isprintable() for character in reference):
        return CommitmentResultCode.proof_reference_invalid
    return None


def _preconditions_satisfied(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    commitment: Commitment,
    *,
    scope: str,
    task_type: str | None,
) -> bool:
    for precondition in commitment.preconditions:
        belief = store.get_belief(precondition.belief_id)
        if belief is None:
            return False
        result = retrieve_beliefs(
            store,
            policy,
            agent_id,
            RetrievalRequest(
                entity=belief.entity,
                attribute=belief.attribute,
                scope=scope,
                task_type=task_type,
            ),
        )
        admitted_ids = {item.belief.id for item in result.items}
        admitted_ids.update(
            belief_id
            for decision in result.deduplications
            for belief_id in decision.dropped_ids
        )
        if precondition.belief_id not in admitted_ids:
            return False
        if precondition.require_uncontradicted and len({
            item.belief.value for item in result.items
        }) > 1:
            return False
    return True


def add_commitment(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: CommitmentCreateRequest,
) -> CommitmentMutationResult:
    agent, error = _agent_for_operation(
        policy, agent_id, CommitmentOperation.create
    )
    if error == CommitmentResultCode.agent_unknown:
        return _mutation_failure(error, "unknown policy agent")
    if error == CommitmentResultCode.operation_not_permitted:
        return _mutation_failure(error, "agent lacks commitment create permission")
    if request.scope is None:
        return _mutation_failure(
            CommitmentResultCode.scope_context_missing,
            "explicit task scope is required",
        )
    _, effective = _scope_sets(request.scope, None, agent.allowed_scopes)
    if request.scope not in effective:
        return _mutation_failure(
            CommitmentResultCode.scope_denied,
            "commitment scope is not authorized for this agent",
        )
    commitment = store.add_commitment(request, created_by_agent_id=agent_id)
    return CommitmentMutationResult(
        ok=True,
        code=CommitmentResultCode.commitment_created,
        message="commitment created",
        commitment=commitment,
    )


def list_commitments(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: CommitmentListRequest,
) -> CommitmentListResult:
    agent = policy.agents.get(agent_id)
    if agent is None:
        return CommitmentListResult(
            authorized=False,
            code=CommitmentResultCode.agent_unknown,
            commitments=[],
            exclusions=[],
        )
    if request.scope is None:
        return CommitmentListResult(
            authorized=False,
            code=CommitmentResultCode.scope_context_missing,
            commitments=[],
            exclusions=[],
        )
    active, effective = _scope_sets(
        request.scope, request.task_type, agent.allowed_scopes
    )
    return CommitmentListResult(
        authorized=True,
        code=CommitmentResultCode.commitments_listed,
        commitments=store.list_commitments(scopes=effective),
        exclusions=_scope_exclusions(store, active, effective),
    )


def transition_commitment(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: CommitmentTransitionRequest,
) -> CommitmentMutationResult:
    agent, error = _agent_for_operation(
        policy, agent_id, CommitmentOperation.transition
    )
    if error == CommitmentResultCode.agent_unknown:
        return _mutation_failure(error, "unknown policy agent")
    if error == CommitmentResultCode.operation_not_permitted:
        return _mutation_failure(error, "agent lacks commitment transition permission")
    if request.scope is None:
        return _mutation_failure(
            CommitmentResultCode.scope_context_missing,
            "explicit task scope is required",
        )
    try:
        target = CommitmentState(request.target_state)
    except ValueError:
        return _mutation_failure(
            CommitmentResultCode.state_unknown, "unknown commitment state"
        )

    commitment = store.get_commitment(request.commitment_id)
    if commitment is None:
        return _mutation_failure(
            CommitmentResultCode.commitment_not_found, "unknown commitment id"
        )
    _, effective = _scope_sets(
        request.scope, request.task_type, agent.allowed_scopes
    )
    if commitment.scope not in effective:
        return _mutation_failure(
            CommitmentResultCode.scope_denied,
            "commitment is outside the authorized task scope",
        )
    if commitment.created_by_agent_id != agent_id:
        return _mutation_failure(
            CommitmentResultCode.managing_agent_required,
            "only the commitment's managing agent may transition it",
        )
    if target == CommitmentState.overdue:
        return _mutation_failure(
            CommitmentResultCode.overdue_scan_required,
            "overdue is assigned only by an authorized deterministic scan",
        )
    if not _transition_allowed(commitment.state, target):
        return _mutation_failure(
            CommitmentResultCode.transition_invalid,
            "transition is not allowed from the current state",
        )
    if request.as_of < commitment.updated_at:
        return _mutation_failure(
            CommitmentResultCode.transition_invalid,
            "transition timestamp must not move backward",
        )

    proof_problem = _proof_problem(request.proof_reference)
    if proof_problem is not None:
        return _mutation_failure(proof_problem, "proof reference is invalid")
    if target != CommitmentState.fulfilled and request.proof_reference is not None:
        return _mutation_failure(
            CommitmentResultCode.proof_not_applicable,
            "proof may be supplied only for fulfillment",
        )
    if target == CommitmentState.fulfilled:
        if commitment.proof_required and request.proof_reference is None:
            return _mutation_failure(
                CommitmentResultCode.proof_required,
                "proof is required for fulfillment",
            )
        if not _preconditions_satisfied(
            store,
            policy,
            agent_id,
            commitment,
            scope=request.scope,
            task_type=request.task_type,
        ):
            return _mutation_failure(
                CommitmentResultCode.precondition_unsatisfied,
                "one or more commitment preconditions are unsatisfied",
            )
    transitioned = store.update_commitment_state(
        commitment.id,
        expected_state=commitment.state,
        target_state=target,
        updated_at=request.as_of,
        proof_reference=request.proof_reference,
    )
    return CommitmentMutationResult(
        ok=True,
        code=CommitmentResultCode.commitment_transitioned,
        message="commitment transitioned",
        commitment=transitioned,
    )


def surface_overdue(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: OverdueScanRequest,
) -> OverdueScanResult:
    agent, error = _agent_for_operation(
        policy, agent_id, CommitmentOperation.scan_overdue
    )
    if error is not None:
        return OverdueScanResult(
            authorized=False,
            code=error,
            as_of=request.as_of,
            overdue=[],
            promoted_count=0,
            exclusions=[],
        )
    if request.scope is None:
        return OverdueScanResult(
            authorized=False,
            code=CommitmentResultCode.scope_context_missing,
            as_of=request.as_of,
            overdue=[],
            promoted_count=0,
            exclusions=[],
        )
    active, effective = _scope_sets(
        request.scope, request.task_type, agent.allowed_scopes
    )
    candidates = store.list_commitments(
        scopes=effective,
        states=[CommitmentState.open, CommitmentState.waiting],
    )
    promoted_count = 0
    for commitment in candidates:
        if request.as_of <= commitment.deadline:
            continue
        if not _transition_allowed(commitment.state, CommitmentState.overdue):
            continue
        store.update_commitment_state(
            commitment.id,
            expected_state=commitment.state,
            target_state=CommitmentState.overdue,
            updated_at=request.as_of,
            proof_reference=None,
        )
        promoted_count += 1

    overdue = store.list_commitments(
        scopes=effective, states=[CommitmentState.overdue]
    )
    return OverdueScanResult(
        authorized=True,
        code=CommitmentResultCode.overdue_scan_completed,
        as_of=request.as_of,
        overdue=overdue,
        promoted_count=promoted_count,
        exclusions=_scope_exclusions(store, active, effective),
    )
