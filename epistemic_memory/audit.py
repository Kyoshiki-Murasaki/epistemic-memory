"""Immutable M7 trace construction, historical counterfactuals, and explain."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Iterable, Mapping, Optional

from .assemble import _build_conflicts, _permissions
from .models import (
    AssembledContext,
    AssemblyAuditSnapshot,
    AuditOperation,
    AuditOutcome,
    AuditPayload,
    AuditTrace,
    Belief,
    BeliefEvidenceSnapshot,
    ConflictChange,
    ConflictGroup,
    ConflictStateSnapshot,
    CounterfactualCode,
    CounterfactualResult,
    CurrentBeliefFollowUp,
    EpistemicStatus,
    EvidenceUseSnapshot,
    ExplainRequest,
    ExplainResult,
    ExplainResultCode,
    GateAuditSnapshot,
    GateDecision,
    GateResult,
    MemoryOperation,
    MutationAuditSnapshot,
    PermissionChange,
    PermissionEntry,
    PolicySnapshot,
    Proposal,
    ProposalAuditSnapshot,
    ProposalState,
    RetrievedBelief,
    SessionMode,
    Source,
    StructuralFollowUpCode,
    TrustPolicy,
)
from .policy import PolicyEvaluationError, gate as evaluate_gate, resolve_conflict
from .rendering import safe_text
from .retrieve import authorized_task_scopes
from .store import Store


class AuditPersistenceError(RuntimeError):
    """Required audit persistence failed inside an atomic public operation."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def policy_snapshot(policy: TrustPolicy) -> PolicySnapshot:
    encoded = canonical_json(policy.model_dump(mode="json")).encode("utf-8")
    return PolicySnapshot(
        version=policy.version,
        fingerprint=hashlib.sha256(encoded).hexdigest(),
    )


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def belief_snapshot(belief: Belief, source: Source) -> BeliefEvidenceSnapshot:
    if belief.id is None:
        raise ValueError("audit evidence requires a persisted belief ID")
    return BeliefEvidenceSnapshot(
        belief_id=belief.id,
        entity=belief.entity,
        attribute=belief.attribute,
        value=belief.value,
        status=belief.status,
        scope=belief.scope,
        source_id=source.id,
        source_type=source.type,
        source_label=source.label,
        event_id=belief.event_id,
        supersedes_id=belief.supersedes_id,
        decision_type=belief.decision_type,
        valid_from=belief.valid_from,
        created_at=belief.created_at,
        was_structurally_current=belief.is_current is True,
    )


def snapshots_for_beliefs(
    store: Store, beliefs: Iterable[Belief]
) -> list[BeliefEvidenceSnapshot]:
    snapshots: list[BeliefEvidenceSnapshot] = []
    for belief in beliefs:
        current_view = store.get_belief(belief.id) if belief.id is not None else None
        if current_view is None:
            raise ValueError("audit evidence disappeared before trace persistence")
        source = store.get_source(current_view.source_id)
        if source is None:
            raise ValueError("audit evidence source disappeared before trace persistence")
        snapshots.append(belief_snapshot(current_view, source))
    return snapshots


def proposal_snapshot(proposal: Proposal) -> ProposalAuditSnapshot:
    return ProposalAuditSnapshot(
        proposal_id=proposal.id,
        source_event_id=proposal.source_event_id,
        source_id=proposal.source_id,
        source_type=proposal.source_type,
        entity=proposal.entity,
        attribute=proposal.attribute,
        value=proposal.value,
        proposed_status=proposal.proposed_status,
        effective_status=proposal.effective_status,
        scope=proposal.scope,
        decision_type=proposal.decision_type,
        creator_agent_id=proposal.creator_agent_id,
        policy_version=proposal.policy_version,
        policy_fingerprint=proposal.policy_fingerprint,
        expected_current_belief_id=proposal.expected_current_belief_id,
        expected_current_absent=proposal.expected_current_absent,
        state=proposal.state,
    )


def _evidence_use(
    item: RetrievedBelief, conflicts: list[ConflictGroup]
) -> EvidenceUseSnapshot:
    group_id = next(
        (
            conflict.group_id
            for conflict in conflicts
            if item.belief.id in conflict.belief_ids
        ),
        None,
    )
    return EvidenceUseSnapshot(
        evidence=belief_snapshot(item.belief, item.source),
        rank=item.rank,
        rank_factors=item.rank_factors,
        admitted_by=item.admitted_by,
        conflict_group_id=group_id,
    )


def _conflict_state_from_group(group: ConflictGroup) -> ConflictStateSnapshot:
    return ConflictStateSnapshot(
        group_id=group.group_id,
        belief_ids=group.belief_ids,
        winner_id=group.winner_id,
        rule_id=group.rule_id,
        conflicted=True,
    )


def _gate_conflict_state(
    items: list[RetrievedBelief], decision_type: Optional[str], policy: TrustPolicy
) -> Optional[ConflictStateSnapshot]:
    if not items or not decision_type:
        return None
    belief_ids = [item.belief.id for item in items]
    values = {item.belief.value for item in items}
    if len(values) < 2:
        return None
    source_types = {item.source.id: item.source.type for item in items}
    try:
        resolution = resolve_conflict(
            [item.belief for item in items], decision_type, policy, source_types
        )
        return ConflictStateSnapshot(
            group_id=f"conflict:{items[0].belief.entity}:{items[0].belief.attribute}",
            belief_ids=belief_ids,
            winner_id=resolution.winner.id,
            rule_id=resolution.rule_id,
            conflicted=True,
        )
    except PolicyEvaluationError:
        return ConflictStateSnapshot(
            group_id=f"conflict:{items[0].belief.entity}:{items[0].belief.attribute}",
            belief_ids=belief_ids,
            winner_id=None,
            rule_id="R-CONFLICT-UNCONFIGURED-001",
            conflicted=True,
        )


def _permission_map(
    permissions: Iterable[PermissionEntry],
) -> dict[tuple[str, str, str], PermissionEntry]:
    return {
        (entry.entity, entry.attribute, entry.action): entry for entry in permissions
    }


def _conflict_map(
    conflicts: Iterable[ConflictGroup],
) -> dict[str, ConflictStateSnapshot]:
    return {
        conflict.group_id: _conflict_state_from_group(conflict)
        for conflict in conflicts
    }


def assembly_counterfactuals(
    result: AssembledContext,
    policy: TrustPolicy,
    agent_id: str,
) -> list[CounterfactualResult]:
    """Precompute policy sensitivity using only the historical included set."""
    original_permissions = _permission_map(result.permissions)
    original_conflicts = _conflict_map(result.conflicts)
    outcomes: list[CounterfactualResult] = []
    for selected in result.items:
        remaining = [
            item for item in result.items if item.belief.id != selected.belief.id
        ]
        alternate_conflicts = _build_conflicts(remaining, policy)
        alternate_permissions = _permissions(remaining, policy, agent_id)
        alternate_permission_map = _permission_map(alternate_permissions)
        alternate_conflict_map = _conflict_map(alternate_conflicts)

        permission_changes: list[PermissionChange] = []
        for key in sorted(set(original_permissions) | set(alternate_permission_map)):
            before = original_permissions.get(key)
            after = alternate_permission_map.get(key)
            before_shape = (
                before.decision,
                before.gate.rule_ids,
                before.gate.reason_codes,
            ) if before else None
            after_shape = (
                after.decision,
                after.gate.rule_ids,
                after.gate.reason_codes,
            ) if after else None
            if before_shape == after_shape:
                continue
            permission_changes.append(PermissionChange(
                entity=key[0],
                attribute=key[1],
                action=key[2],
                before=before.decision if before else None,
                after=after.decision if after else None,
                before_rule_ids=before.gate.rule_ids if before else [],
                after_rule_ids=after.gate.rule_ids if after else [],
                before_reason_codes=before.gate.reason_codes if before else [],
                after_reason_codes=after.gate.reason_codes if after else [],
            ))

        conflict_changes: list[ConflictChange] = []
        for group_id in sorted(set(original_conflicts) | set(alternate_conflict_map)):
            before = original_conflicts.get(group_id)
            after = alternate_conflict_map.get(group_id)
            if (
                before.model_dump(mode="json") if before else None
            ) == (
                after.model_dump(mode="json") if after else None
            ):
                continue
            conflict_changes.append(ConflictChange(
                group_id=group_id, before=before, after=after
            ))

        changed = bool(permission_changes or conflict_changes)
        outcomes.append(CounterfactualResult(
            belief_id=selected.belief.id,
            code=(CounterfactualCode.changed if changed else CounterfactualCode.no_change),
            remaining_belief_ids=[item.belief.id for item in remaining],
            permission_changes=permission_changes,
            conflict_changes=conflict_changes,
        ))
    return outcomes


def gate_counterfactuals(
    result: GateResult,
    items: list[RetrievedBelief],
    policy: TrustPolicy,
    agent_id: str,
) -> list[CounterfactualResult]:
    original_conflict = _gate_conflict_state(items, result.decision_type, policy)
    outcomes: list[CounterfactualResult] = []
    for selected in items:
        remaining = [
            item for item in items if item.belief.id != selected.belief.id
        ]
        source_types = {item.source.id: item.source.type for item in remaining}
        alternate = evaluate_gate(
            result.action,
            [item.belief for item in remaining],
            policy,
            source_types,
            agent_id=agent_id,
        )
        alternate_conflict = _gate_conflict_state(
            remaining, result.decision_type, policy
        )
        conflict_changes: list[ConflictChange] = []
        before_shape = (
            original_conflict.model_dump(mode="json") if original_conflict else None
        )
        after_shape = (
            alternate_conflict.model_dump(mode="json") if alternate_conflict else None
        )
        if before_shape != after_shape:
            group_id = (
                original_conflict.group_id
                if original_conflict is not None
                else alternate_conflict.group_id
            )
            conflict_changes.append(ConflictChange(
                group_id=group_id,
                before=original_conflict,
                after=alternate_conflict,
            ))
        changed = (
            result.decision != alternate.decision
            or result.rule_ids != alternate.rule_ids
            or result.reason_codes != alternate.reason_codes
            or bool(conflict_changes)
        )
        outcomes.append(CounterfactualResult(
            belief_id=selected.belief.id,
            code=(CounterfactualCode.changed if changed else CounterfactualCode.no_change),
            remaining_belief_ids=[item.belief.id for item in remaining],
            gate_before=result.decision,
            gate_after=alternate.decision,
            reason_codes_before=result.reason_codes,
            reason_codes_after=alternate.reason_codes,
            conflict_changes=conflict_changes,
        ))
    return outcomes


def _trace(
    *,
    trace_id: str,
    session_id: str,
    session_mode: SessionMode,
    agent_id: str,
    approval_actor_id: Optional[str],
    active_scope: str,
    task_type: Optional[str],
    operation: AuditOperation,
    outcome: AuditOutcome,
    result_code: str,
    reason_codes: Iterable[str],
    rule_ids: Iterable[str],
    policy: TrustPolicy,
    payload: AuditPayload,
    persisted: bool,
    created_at: datetime,
) -> AuditTrace:
    return AuditTrace(
        trace_id=trace_id,
        session_id=session_id,
        session_mode=session_mode,
        agent_id=agent_id,
        approval_actor_id=approval_actor_id,
        active_scope=active_scope,
        task_type=task_type,
        operation=operation,
        outcome=outcome,
        result_code=result_code,
        reason_codes=_unique(reason_codes),
        rule_ids=_unique(rule_ids),
        policy=policy_snapshot(policy),
        payload=payload,
        persisted=persisted,
        created_at=created_at,
    )


def build_assembly_trace(
    result: AssembledContext,
    *,
    trace_id: str,
    session_id: str,
    session_mode: SessionMode,
    agent_id: str,
    policy: TrustPolicy,
    persisted: bool,
    created_at: datetime,
) -> AuditTrace:
    if result.receipt is None:
        raise ValueError("successful assembly audit requires a memory receipt")
    snapshot = AssemblyAuditSnapshot(
        request=result.request,
        evidence=[_evidence_use(item, result.conflicts) for item in result.items],
        conflicts=result.conflicts,
        permissions=result.permissions,
        receipt=result.receipt,
        rendered_receipt=result.rendered_receipt,
        tokens_injected=result.tokens_injected,
        token_budget=result.token_budget,
    )
    return _trace(
        trace_id=trace_id,
        session_id=session_id,
        session_mode=session_mode,
        agent_id=agent_id,
        approval_actor_id=None,
        active_scope=result.request.scope,
        task_type=result.request.task_type,
        operation=AuditOperation.assemble,
        outcome=AuditOutcome.completed,
        result_code="context_assembled",
        reason_codes=[
            *(conflict.reason_code for conflict in result.conflicts),
            *(code for entry in result.permissions for code in entry.gate.reason_codes),
            *(item.reason_code for item in result.receipt.exclusions),
        ],
        rule_ids=result.receipt.rule_ids,
        policy=policy,
        payload=AuditPayload(
            assembly=snapshot,
            counterfactuals=assembly_counterfactuals(result, policy, agent_id),
        ),
        persisted=persisted,
        created_at=created_at,
    )


def build_gate_trace(
    result: GateResult,
    items: list[RetrievedBelief],
    exclusions,
    *,
    entity: str,
    scope: str,
    task_type: Optional[str],
    trace_id: str,
    session_id: str,
    session_mode: SessionMode,
    agent_id: str,
    policy: TrustPolicy,
    persisted: bool,
    created_at: datetime,
) -> AuditTrace:
    snapshot = GateAuditSnapshot(
        action=result.action,
        entity=entity,
        request_scope=scope,
        task_type=task_type,
        evidence=[belief_snapshot(item.belief, item.source) for item in items],
        result=result,
        exclusions=exclusions,
        conflict=_gate_conflict_state(items, result.decision_type, policy),
    )
    return _trace(
        trace_id=trace_id,
        session_id=session_id,
        session_mode=session_mode,
        agent_id=agent_id,
        approval_actor_id=None,
        active_scope=scope,
        task_type=task_type,
        operation=AuditOperation.gate,
        outcome=AuditOutcome.completed,
        result_code=result.decision.value,
        reason_codes=result.reason_codes,
        rule_ids=result.rule_ids,
        policy=policy,
        payload=AuditPayload(
            gate=snapshot,
            counterfactuals=gate_counterfactuals(result, items, policy, agent_id),
        ),
        persisted=persisted,
        created_at=created_at,
    )


def build_mutation_trace(
    mutation: MutationAuditSnapshot,
    *,
    trace_id: str,
    session_id: str,
    session_mode: SessionMode,
    agent_id: str,
    approval_actor_id: Optional[str],
    active_scope: str,
    task_type: Optional[str],
    operation: AuditOperation,
    outcome: AuditOutcome,
    result_code: str,
    reason_codes: Iterable[str],
    rule_ids: Iterable[str],
    policy: TrustPolicy,
    created_at: datetime,
) -> AuditTrace:
    return _trace(
        trace_id=trace_id,
        session_id=session_id,
        session_mode=session_mode,
        agent_id=agent_id,
        approval_actor_id=approval_actor_id,
        active_scope=active_scope,
        task_type=task_type,
        operation=operation,
        outcome=outcome,
        result_code=result_code,
        reason_codes=reason_codes,
        rule_ids=rule_ids,
        policy=policy,
        payload=AuditPayload(mutation=mutation),
        persisted=True,
        created_at=created_at,
    )


def _trace_evidence(trace: AuditTrace) -> list[BeliefEvidenceSnapshot]:
    if trace.payload.assembly is not None:
        return [item.evidence for item in trace.payload.assembly.evidence]
    if trace.payload.gate is not None:
        return trace.payload.gate.evidence
    if trace.payload.mutation is not None:
        return trace.payload.mutation.beliefs
    return []


def _trace_content_scopes(trace: AuditTrace) -> list[str]:
    scopes = [item.scope for item in _trace_evidence(trace)]
    if trace.payload.mutation is not None:
        scopes.extend(item.scope for item in trace.payload.mutation.proposals)
        if trace.payload.mutation.target_belief is not None:
            scopes.append(trace.payload.mutation.target_belief.scope)
    return list(dict.fromkeys(scopes))


def _unavailable(request: ExplainRequest) -> ExplainResult:
    return ExplainResult(
        authorized=False,
        code=ExplainResultCode.trace_unavailable,
        trace_id=request.trace_id,
    )


def _current_follow_up(
    store: Store,
    evidence: BeliefEvidenceSnapshot,
    visible_scopes: list[str],
) -> CurrentBeliefFollowUp:
    stored = store.get_belief(evidence.belief_id)
    if stored is None or stored.scope not in visible_scopes:
        return CurrentBeliefFollowUp(
            belief_id=evidence.belief_id,
            code=StructuralFollowUpCode.follow_up_unavailable,
        )
    current = stored
    saw_successor = False
    while True:
        successors = store.belief_successors(current.id)
        if not successors:
            break
        if len(successors) != 1:
            return CurrentBeliefFollowUp(
                belief_id=evidence.belief_id,
                code=StructuralFollowUpCode.follow_up_unavailable,
            )
        successor = successors[0]
        if (
            successor.source_id != evidence.source_id
            or successor.key != (evidence.entity, evidence.attribute)
            or successor.scope not in visible_scopes
        ):
            return CurrentBeliefFollowUp(
                belief_id=evidence.belief_id,
                code=StructuralFollowUpCode.follow_up_unavailable,
            )
        saw_successor = True
        current = successor

    if saw_successor:
        code = (
            StructuralFollowUpCode.superseded_by_retraction
            if current.status == EpistemicStatus.retracted
            else StructuralFollowUpCode.superseded_by_later_same_source_belief
        )
    elif current.status == EpistemicStatus.retracted:
        code = StructuralFollowUpCode.current_retraction
    elif current.status == EpistemicStatus.do_not_use:
        code = StructuralFollowUpCode.current_do_not_use
    elif current.status == EpistemicStatus.disputed:
        code = StructuralFollowUpCode.current_disputed
    elif current.status == EpistemicStatus.superseded:
        code = StructuralFollowUpCode.current_superseded_status
    else:
        code = StructuralFollowUpCode.still_structurally_current
    return CurrentBeliefFollowUp(
        belief_id=evidence.belief_id,
        code=code,
        current_belief_id=current.id,
        current_status=current.status,
    )


def _render_counterfactual(counterfactual: Optional[CounterfactualResult]) -> list[str]:
    if counterfactual is None:
        return ["- not requested"]
    lines = [
        f"- code:{safe_text(counterfactual.code.value)} "
        f"belief:{counterfactual.belief_id if counterfactual.belief_id else 'none'}",
        "- remaining:" + (
            ",".join(str(value) for value in counterfactual.remaining_belief_ids)
            or "none"
        ),
    ]
    if counterfactual.gate_before is not None or counterfactual.gate_after is not None:
        lines.append(
            "- gate:before="
            + safe_text(counterfactual.gate_before.value if counterfactual.gate_before else "none")
            + " after="
            + safe_text(counterfactual.gate_after.value if counterfactual.gate_after else "none")
        )
        lines.append(
            "- reasons:before="
            + (",".join(safe_text(value) for value in counterfactual.reason_codes_before)
               or safe_text("none"))
            + " after="
            + (",".join(safe_text(value) for value in counterfactual.reason_codes_after)
               or safe_text("none"))
        )
    for change in counterfactual.permission_changes:
        lines.append(
            f"- permission action:{safe_text(change.action)} "
            f"before:{safe_text(change.before.value if change.before else 'none')} "
            f"after:{safe_text(change.after.value if change.after else 'none')} "
            "before_rules:"
            + (",".join(safe_text(value) for value in change.before_rule_ids)
               or safe_text("none"))
            + " after_rules:"
            + (",".join(safe_text(value) for value in change.after_rule_ids)
               or safe_text("none"))
            + " before_reasons:"
            + (",".join(safe_text(value) for value in change.before_reason_codes)
               or safe_text("none"))
            + " after_reasons:"
            + (",".join(safe_text(value) for value in change.after_reason_codes)
               or safe_text("none"))
        )
    for change in counterfactual.conflict_changes:
        def describe(value: Optional[ConflictStateSnapshot]) -> str:
            if value is None:
                return "none"
            return (
                f"beliefs={','.join(str(item) for item in value.belief_ids)};"
                f"winner={value.winner_id or 'none'};rule={value.rule_id or 'none'}"
            )
        lines.append(
            f"- conflict group:{safe_text(change.group_id)} "
            f"before:{safe_text(describe(change.before))} "
            f"after:{safe_text(describe(change.after))}"
        )
    return lines


def render_explanation(
    trace: AuditTrace,
    counterfactual: Optional[CounterfactualResult],
    follow_up: list[CurrentBeliefFollowUp],
) -> str:
    lines = [
        "AUDIT EXPLANATION",
        f"trace:{safe_text(trace.trace_id)} operation:{safe_text(trace.operation.value)} "
        f"result:{safe_text(trace.result_code)}",
        f"agent:{safe_text(trace.agent_id)} session:{safe_text(trace.session_id)} "
        f"mode:{safe_text(trace.session_mode.value)}",
        f"scope:{safe_text(trace.active_scope)} "
        f"task_type:{safe_text(trace.task_type or 'none')} "
        f"at:{safe_text(trace.created_at.isoformat())}",
        f"policy:version={trace.policy.version} fingerprint:{safe_text(trace.policy.fingerprint)}",
        "reasons:" + (
            ",".join(safe_text(value) for value in trace.reason_codes)
            or safe_text("none")
        ),
        "rules:" + (
            ",".join(safe_text(value) for value in trace.rule_ids)
            or safe_text("none")
        ),
        "",
        "HISTORICAL REASONING (TIME OF DECISION)",
        "beliefs:",
    ]
    evidence = _trace_evidence(trace)
    if evidence:
        for item in evidence:
            lines.append(
                f"- belief:{item.belief_id} status:{safe_text(item.status.value)} "
                f"source:{safe_text(item.source_id)}/{safe_text(item.source_type)} "
                f"scope:{safe_text(item.scope)} key:{safe_text(item.entity)}."
                f"{safe_text(item.attribute)} value:{safe_text(item.value)} "
                f"current_at_decision:{str(item.was_structurally_current).lower()}"
            )
    else:
        lines.append("- none")

    if trace.payload.assembly is not None:
        assembly = trace.payload.assembly
        lines.append("conflicts:")
        lines.extend(
            f"- group:{safe_text(group.group_id)} beliefs:"
            f"{','.join(str(value) for value in group.belief_ids)} "
            f"winner:{group.winner_id if group.winner_id else 'none'} "
            f"rule:{safe_text(group.rule_id)}"
            for group in assembly.conflicts
        )
        if not assembly.conflicts:
            lines.append("- none")
        lines.append("permissions:")
        lines.extend(
            f"- action:{safe_text(entry.action)} decision:{safe_text(entry.decision.value)} "
            f"rules:{','.join(safe_text(rule) for rule in entry.gate.rule_ids) or safe_text('none')}"
            for entry in assembly.permissions
        )
        if not assembly.permissions:
            lines.append("- none")
        lines.append(
            f"memory_receipt:included={len(assembly.receipt.included)} "
            f"tokens={assembly.receipt.tokens.used}/{assembly.receipt.tokens.budget} "
            f"method:{safe_text(assembly.receipt.tokens.method)}"
        )
    elif trace.payload.gate is not None:
        gate = trace.payload.gate
        lines.append(
            f"gate:action={safe_text(gate.action)} decision="
            f"{safe_text(gate.result.decision.value)} risk="
            f"{safe_text(gate.result.risk_tier.value if gate.result.risk_tier else 'none')}"
        )
        for detail in gate.result.details:
            lines.append(
                f"- reason:{safe_text(detail.code)} rule:"
                f"{safe_text(detail.rule_id or 'none')} detail:{safe_text(detail.message)}"
            )
    else:
        mutation = trace.payload.mutation
        lines.append(
            "mutation:events="
            + ",".join(str(value) for value in mutation.event_ids)
            + " beliefs="
            + ",".join(str(value) for value in mutation.belief_ids)
            + " proposals="
            + ",".join(safe_text(value) for value in mutation.proposal_ids)
            + " commitments="
            + ",".join(str(value) for value in mutation.commitment_ids)
            + " artifacts="
            + ",".join(str(value) for value in mutation.artifact_ids)
            + " dependencies="
            + ",".join(str(value) for value in mutation.dependency_ids)
        )
        if mutation.previous_state is not None or mutation.new_state is not None:
            lines.append(
                f"transition:before={safe_text(mutation.previous_state or 'none')} "
                f"after={safe_text(mutation.new_state or 'none')}"
            )
        for proposal in mutation.proposals:
            lines.append(
                f"proposal:{safe_text(proposal.proposal_id)} "
                f"source:{safe_text(proposal.source_id)}/{safe_text(proposal.source_type)} "
                f"scope:{safe_text(proposal.scope)} key:{safe_text(proposal.entity)}."
                f"{safe_text(proposal.attribute)} value:{safe_text(proposal.value)} "
                f"proposed:{safe_text(proposal.proposed_status.value)} "
                f"effective:{safe_text(proposal.effective_status.value)} "
                f"state:{safe_text(proposal.state.value)} "
                f"reviewed_policy:version={proposal.policy_version} "
                f"fingerprint:{safe_text(proposal.policy_fingerprint)}"
            )
        if mutation.target_belief is not None:
            lines.append(
                f"correction_target:belief={mutation.target_belief.belief_id} "
                f"status:{safe_text(mutation.target_belief.status.value)}"
            )
        for impact in mutation.visible_impacts:
            lines.append(
                f"impact:artifact={impact.artifact_id} depth={impact.depth} "
                f"before:{safe_text(impact.previous_state.value)} "
                f"after:{safe_text(impact.new_state.value)} "
                f"reason:{safe_text(impact.reason_code.value)} "
                f"rule:{safe_text(impact.rule_id)}"
            )
        if mutation.observed_current_belief_ids:
            lines.append(
                "observed_current_same_source:"
                + ",".join(str(value) for value in mutation.observed_current_belief_ids)
            )
        if mutation.hidden_impact_count:
            lines.append(
                f"hidden_impacts:count={mutation.hidden_impact_count} "
                f"rule:{safe_text('M6-HIDDEN-IMPACTS-001')}"
            )

    lines.extend(["", "COUNTERFACTUAL (POLICY SENSITIVITY)"])
    lines.extend(_render_counterfactual(counterfactual))
    lines.extend(["", "CURRENT FOLLOW-UP (NOT PART OF ORIGINAL DECISION)"])
    if follow_up:
        lines.extend(
            f"- belief:{item.belief_id} state:{safe_text(item.code.value)} "
            f"current_belief:{item.current_belief_id or 'none'} "
            f"status:{safe_text(item.current_status.value if item.current_status else 'none')}"
            for item in follow_up
        )
    else:
        lines.append("- none")
    return "\n".join(lines)


def explain(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: ExplainRequest,
    transient_traces: Mapping[str, AuditTrace],
) -> ExplainResult:
    if request.scope is None:
        return ExplainResult(
            authorized=False,
            code=ExplainResultCode.scope_context_missing,
            trace_id=request.trace_id,
        )
    agent = policy.agents.get(agent_id)
    if agent is None or MemoryOperation.explain not in agent.memory_operations:
        return _unavailable(request)
    visible_scopes = authorized_task_scopes(
        request.scope, request.task_type, agent.allowed_scopes
    )
    try:
        trace = transient_traces.get(request.trace_id)
        if trace is not None:
            trace = trace.model_copy(deep=True)
        if trace is None:
            trace = store.get_audit_trace(request.trace_id)
    except Exception:
        return _unavailable(request)
    if trace is None or trace.active_scope not in visible_scopes:
        return _unavailable(request)
    evidence = _trace_evidence(trace)
    if any(scope not in visible_scopes for scope in _trace_content_scopes(trace)):
        return _unavailable(request)

    counterfactual: Optional[CounterfactualResult] = None
    if request.belief_id is not None:
        if trace.operation not in {AuditOperation.assemble, AuditOperation.gate}:
            counterfactual = CounterfactualResult(
                belief_id=request.belief_id,
                code=CounterfactualCode.counterfactual_not_applicable,
            )
        else:
            counterfactual = next(
                (
                    outcome
                    for outcome in trace.payload.counterfactuals
                    if outcome.belief_id == request.belief_id
                ),
                CounterfactualResult(
                    belief_id=request.belief_id,
                    code=CounterfactualCode.belief_not_in_trace,
                ),
            )
    follow_up = [
        _current_follow_up(store, item, visible_scopes) for item in evidence
    ]
    rendered = render_explanation(trace, counterfactual, follow_up)
    return ExplainResult(
        authorized=True,
        code=ExplainResultCode.explained,
        trace_id=request.trace_id,
        trace=trace,
        counterfactual=counterfactual,
        current_follow_up=follow_up,
        rendered=rendered,
    )
