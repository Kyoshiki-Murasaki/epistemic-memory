"""M6 artifact registration, explicit dependency DAG, and correction propagation."""

from __future__ import annotations

from collections import deque
from datetime import datetime

from .ingest import ingest_event
from .models import (
    Artifact,
    ArtifactExecutionState,
    ArtifactImpact,
    ArtifactKind,
    ArtifactPropagationState,
    ArtifactRegistrationRequest,
    ArtifactRegistrationResult,
    CandidateBelief,
    CorrectionKind,
    CorrectionRequest,
    CorrectionResult,
    DependencyEndpointKind,
    DependencyRegistrationRequest,
    DependencyRegistrationResult,
    EpistemicStatus,
    HiddenImpactSummary,
    M6ResultCode,
    MemoryOperation,
    TrustPolicy,
)
from .retrieve import authorized_task_scopes, scope_allowed
from .store import Store


def _artifact_failure(code: M6ResultCode, message: str) -> ArtifactRegistrationResult:
    return ArtifactRegistrationResult(ok=False, code=code, message=message)


def _dependency_failure(
    code: M6ResultCode, message: str
) -> DependencyRegistrationResult:
    return DependencyRegistrationResult(ok=False, code=code, message=message)


def _correction_failure(
    code: M6ResultCode, message: str, as_of: datetime
) -> CorrectionResult:
    return CorrectionResult(ok=False, code=code, message=message, as_of=as_of)


def _agent_for_operation(
    policy: TrustPolicy, agent_id: str, operation: MemoryOperation
):
    agent = policy.agents.get(agent_id)
    if agent is None:
        return None, M6ResultCode.agent_unknown
    if operation not in agent.memory_operations:
        return None, M6ResultCode.operation_not_permitted
    return agent, None


def register_artifact(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: ArtifactRegistrationRequest,
    *,
    as_of: datetime,
) -> ArtifactRegistrationResult:
    agent, error = _agent_for_operation(
        policy, agent_id, MemoryOperation.register_artifact
    )
    if error == M6ResultCode.agent_unknown:
        return _artifact_failure(error, "unknown policy agent")
    if error == M6ResultCode.operation_not_permitted:
        return _artifact_failure(error, "agent lacks artifact registration permission")
    if request.scope is None:
        return _artifact_failure(
            M6ResultCode.scope_context_missing, "explicit artifact scope is required"
        )
    if not scope_allowed(request.scope, agent.allowed_scopes):
        return _artifact_failure(
            M6ResultCode.scope_denied, "artifact scope is not authorized for this agent"
        )
    artifact = store.add_artifact(
        request, created_by_agent_id=agent_id, created_at=as_of
    )
    return ArtifactRegistrationResult(
        ok=True,
        code=M6ResultCode.artifact_registered,
        message="artifact registered",
        artifact=artifact,
    )


def _would_create_cycle(store: Store, upstream_id: int, downstream_id: int) -> bool:
    """Adding upstream -> downstream cycles iff downstream already reaches upstream."""
    adjacency = store.artifact_adjacency()
    pending = [downstream_id]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if current == upstream_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(reversed(adjacency.get(current, [])))
    return False


def register_dependency(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: DependencyRegistrationRequest,
    *,
    as_of: datetime,
) -> DependencyRegistrationResult:
    agent, error = _agent_for_operation(
        policy, agent_id, MemoryOperation.register_dependency
    )
    if error == M6ResultCode.agent_unknown:
        return _dependency_failure(error, "unknown policy agent")
    if error == M6ResultCode.operation_not_permitted:
        return _dependency_failure(error, "agent lacks dependency registration permission")
    if request.scope is None:
        return _dependency_failure(
            M6ResultCode.scope_context_missing, "explicit task scope is required"
        )

    downstream = store.get_artifact(request.downstream_artifact_id)
    if downstream is None:
        return _dependency_failure(
            M6ResultCode.artifact_not_found, "downstream artifact does not exist"
        )
    if request.upstream_kind == DependencyEndpointKind.belief:
        upstream = store.get_belief(request.upstream_id)
    else:
        upstream = store.get_artifact(request.upstream_id)
    if upstream is None:
        return _dependency_failure(
            M6ResultCode.dependency_endpoint_not_found, "upstream endpoint does not exist"
        )
    if (
        request.upstream_kind == DependencyEndpointKind.artifact
        and request.upstream_id == request.downstream_artifact_id
    ):
        return _dependency_failure(
            M6ResultCode.dependency_self_edge, "artifact dependency cannot be a self-edge"
        )

    # General agent read permission is checked before returning a structural
    # scope reason. The error never includes endpoint scope values.
    if not all(
        scope_allowed(endpoint.scope, agent.allowed_scopes)
        for endpoint in (upstream, downstream)
    ):
        return _dependency_failure(
            M6ResultCode.scope_denied, "one or more endpoints are outside agent scope"
        )
    if upstream.scope != "global" and upstream.scope != downstream.scope:
        return _dependency_failure(
            M6ResultCode.dependency_scope_incompatible,
            "upstream scope cannot support the downstream artifact scope",
        )
    effective = authorized_task_scopes(
        request.scope, request.task_type, agent.allowed_scopes
    )
    if upstream.scope not in effective or downstream.scope not in effective:
        return _dependency_failure(
            M6ResultCode.scope_denied,
            "one or more endpoints are outside the active task scope",
        )
    existing = store.get_dependency(
        request.upstream_kind,
        request.upstream_id,
        request.downstream_artifact_id,
    )
    if existing is not None:
        return _dependency_failure(
            M6ResultCode.dependency_duplicate, "dependency is already registered"
        )
    if (
        request.upstream_kind == DependencyEndpointKind.artifact
        and _would_create_cycle(
            store, request.upstream_id, request.downstream_artifact_id
        )
    ):
        return _dependency_failure(
            M6ResultCode.dependency_cycle, "dependency would create a cycle"
        )

    dependency = store.add_dependency(
        request.upstream_kind,
        request.upstream_id,
        request.downstream_artifact_id,
        created_by_agent_id=agent_id,
        created_at=as_of,
    )
    return DependencyRegistrationResult(
        ok=True,
        code=M6ResultCode.dependency_registered,
        message="dependency registered",
        dependency=dependency,
    )


def _reachable_artifacts(store: Store, belief_id: int) -> list[tuple[int, int]]:
    """Cycle-safe BFS ordered by shortest depth, then immutable artifact id."""
    pending = deque(
        (artifact_id, 1)
        for artifact_id in store.downstream_artifact_ids(
            DependencyEndpointKind.belief, belief_id
        )
    )
    visited: set[int] = set()
    reached: list[tuple[int, int]] = []
    while pending:
        artifact_id, depth = pending.popleft()
        if artifact_id in visited:
            continue
        visited.add(artifact_id)
        reached.append((depth, artifact_id))
        for child_id in store.downstream_artifact_ids(
            DependencyEndpointKind.artifact, artifact_id
        ):
            if child_id not in visited:
                pending.append((child_id, depth + 1))
    return sorted(reached)


def _propagation_response(
    artifact: Artifact,
) -> tuple[ArtifactPropagationState, M6ResultCode, str]:
    if artifact.kind == ArtifactKind.output:
        return (
            ArtifactPropagationState.stale,
            M6ResultCode.artifact_marked_stale,
            "M6-OUTPUT-STALE-001",
        )
    if artifact.execution_state == ArtifactExecutionState.pending:
        return (
            ArtifactPropagationState.halted,
            M6ResultCode.pending_action_halted,
            "M6-PENDING-ACTION-HALT-001",
        )
    return (
        ArtifactPropagationState.review_required,
        M6ResultCode.executed_action_requires_review,
        "M6-EXECUTED-ACTION-REVIEW-001",
    )


def apply_propagation(
    store: Store,
    belief_id: int,
    *,
    visible_scopes: list[str],
    as_of: datetime,
) -> tuple[list[ArtifactImpact], int, int]:
    """Apply conservative downstream invalidation once per reachable artifact."""
    visible: list[ArtifactImpact] = []
    hidden_count = 0
    reached = _reachable_artifacts(store, belief_id)
    for depth, artifact_id in reached:
        artifact = store.get_artifact(artifact_id)
        if artifact is None:
            raise RuntimeError("dependency graph contains a dangling artifact")
        state, reason_code, rule_id = _propagation_response(artifact)
        updated, previous, changed = store.set_artifact_propagation_state(
            artifact_id, state=state, updated_at=as_of
        )
        if updated.scope in visible_scopes:
            visible.append(ArtifactImpact(
                artifact=updated,
                depth=depth,
                previous_state=previous,
                reason_code=reason_code,
                rule_id=rule_id,
                state_changed=changed,
            ))
        else:
            hidden_count += 1
    return visible, hidden_count, len(reached)


def correct_belief(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: CorrectionRequest,
    *,
    as_of: datetime,
) -> CorrectionResult:
    agent, error = _agent_for_operation(policy, agent_id, MemoryOperation.correct)
    if error == M6ResultCode.agent_unknown:
        return _correction_failure(error, "unknown policy agent", as_of)
    if error == M6ResultCode.operation_not_permitted:
        return _correction_failure(error, "agent lacks correction permission", as_of)
    if request.scope is None:
        return _correction_failure(
            M6ResultCode.scope_context_missing, "explicit task scope is required", as_of
        )

    target = store.get_belief(request.belief_id)
    if target is None:
        return _correction_failure(
            M6ResultCode.target_belief_not_found, "target belief does not exist", as_of
        )
    visible_scopes = authorized_task_scopes(
        request.scope, request.task_type, agent.allowed_scopes
    )
    if target.scope not in visible_scopes:
        return _correction_failure(
            M6ResultCode.scope_denied,
            "target belief is outside the authorized active task scope",
            as_of,
        )
    if request.source_id != target.source_id:
        return _correction_failure(
            M6ResultCode.source_mismatch,
            "cross-source correction cannot supersede a belief",
            as_of,
        )
    if target.is_current is not True:
        return _correction_failure(
            M6ResultCode.target_belief_not_current,
            "target belief is not structurally current",
            as_of,
        )
    dead = {
        EpistemicStatus.superseded,
        EpistemicStatus.retracted,
        EpistemicStatus.do_not_use,
    }
    if target.status in dead:
        return _correction_failure(
            M6ResultCode.invalid_correction,
            "an unusable belief cannot be corrected or retracted again",
            as_of,
        )
    if request.kind == CorrectionKind.correction:
        if request.value is None or request.proposed_status is None:
            return _correction_failure(
                M6ResultCode.invalid_correction,
                "correction requires a replacement value and proposed status",
                as_of,
            )
        if request.proposed_status in dead:
            return _correction_failure(
                M6ResultCode.invalid_correction,
                "correction proposed status must remain usable",
                as_of,
            )
        value = request.value
        proposed_status = request.proposed_status
    else:
        if request.value is not None or request.proposed_status is not None:
            return _correction_failure(
                M6ResultCode.invalid_correction,
                "retraction does not accept a replacement value or proposed status",
                as_of,
            )
        value = target.value
        proposed_status = EpistemicStatus.retracted

    candidate = CandidateBelief(
        entity=target.entity,
        attribute=target.attribute,
        value=value,
        proposed_status=proposed_status,
        scope=target.scope,
        decision_type=target.decision_type,
    )

    def extractor(event, source_type):
        return [candidate]

    try:
        with store.transaction():
            ingested = ingest_event(
                store,
                policy,
                source_id=request.source_id,
                content=request.content,
                scope=target.scope,
                meta={
                    "operation": request.kind.value,
                    "target_belief_id": target.id,
                },
                extractor=extractor,
                as_of=as_of,
                supersede_belief_id=target.id,
            )
            if len(ingested.beliefs) != 1:
                raise RuntimeError("correction ingest did not create exactly one belief")
            replacement = ingested.beliefs[0]
            if replacement.supersedes_id != target.id:
                raise RuntimeError("correction did not supersede the requested target")
            visible, hidden_count, affected_count = apply_propagation(
                store,
                target.id,
                visible_scopes=visible_scopes,
                as_of=as_of,
            )
    except Exception:
        return _correction_failure(
            M6ResultCode.atomic_propagation_failure,
            "correction and propagation rolled back atomically",
            as_of,
        )

    hidden = []
    if hidden_count:
        hidden.append(HiddenImpactSummary(
            reason_code=M6ResultCode.hidden_downstream_impacts,
            rule_id="M6-HIDDEN-IMPACTS-001",
            count=hidden_count,
            safe_detail="downstream artifacts were protected outside response visibility",
        ))
    return CorrectionResult(
        ok=True,
        code=M6ResultCode.correction_applied,
        message="correction applied and downstream artifacts propagated",
        as_of=as_of,
        event=ingested.event,
        belief=replacement,
        visible_impacts=visible,
        hidden_impacts=hidden,
        affected_count=affected_count,
    )
