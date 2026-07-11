"""M7 proposal queue creation, visibility, and approval validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .audit import (
    build_mutation_trace,
    policy_snapshot,
    proposal_snapshot,
    snapshots_for_beliefs,
)
from .ingest import Extractor, materialize_candidate
from .models import (
    Belief,
    AuditOperation,
    AuditOutcome,
    AuditTrace,
    CandidateBelief,
    Event,
    MemoryOperation,
    Proposal,
    ProposalCreateResult,
    ProposalExclusionSummary,
    ProposalListRequest,
    ProposalListResult,
    ProposalResultCode,
    ProposalState,
    MutationAuditSnapshot,
    SessionMode,
    TrustPolicy,
)
from .policy import clamp_status
from .retrieve import active_task_scopes, authorized_task_scopes
from .store import Store


class ProposalWorkflowError(RuntimeError):
    def __init__(self, code: ProposalResultCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProposalDecisionExecution:
    ok: bool
    code: ProposalResultCode
    message: str
    proposal: Proposal
    belief: Optional[Belief]
    trace: AuditTrace


def propose_agent(policy: TrustPolicy, agent_id: str):
    agent = policy.agents.get(agent_id)
    if agent is None:
        raise ProposalWorkflowError(
            ProposalResultCode.agent_unknown, "unknown policy agent"
        )
    if MemoryOperation.propose not in agent.memory_operations:
        raise ProposalWorkflowError(
            ProposalResultCode.operation_not_permitted,
            "agent lacks proposal permission",
        )
    return agent


def approval_scopes(
    policy: TrustPolicy,
    agent_id: str,
    approval_actor_id: Optional[str],
    scope: Optional[str],
    task_type: Optional[str],
) -> tuple[list[str], list[str]]:
    agent = propose_agent(policy, agent_id)
    if not approval_actor_id:
        raise ProposalWorkflowError(
            ProposalResultCode.approval_actor_required,
            "constructor-controlled human approval identity is required",
        )
    if approval_actor_id == agent_id:
        raise ProposalWorkflowError(
            ProposalResultCode.approval_actor_not_distinct,
            "approval actor must be distinct from the policy agent",
        )
    if scope is None:
        raise ProposalWorkflowError(
            ProposalResultCode.scope_context_missing,
            "explicit task scope is required",
        )
    active = active_task_scopes(scope, task_type)
    effective = authorized_task_scopes(scope, task_type, agent.allowed_scopes)
    return active, effective


def create_proposals(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    *,
    source_id: str,
    content: str,
    scope: str,
    meta: Optional[dict],
    extractor: Extractor,
    as_of: datetime,
    creation_trace_id: str,
    proposal_id_factory: Callable[[], str],
) -> ProposalCreateResult:
    agent = propose_agent(policy, agent_id)
    source = store.get_source(source_id)
    if source is None:
        raise ProposalWorkflowError(
            ProposalResultCode.source_invalid, "proposal source does not exist"
        )
    effective_scopes = authorized_task_scopes(scope, None, agent.allowed_scopes)
    if scope not in effective_scopes:
        raise ProposalWorkflowError(
            ProposalResultCode.scope_denied,
            "active proposal scope is not authorized",
        )

    event = store.add_event(Event(
        source_id=source_id,
        content=content,
        scope=scope,
        meta=meta,
        created_at=as_of.isoformat(),
    ))
    candidates = extractor(event, source.type)
    if not isinstance(candidates, list) or any(
        not isinstance(candidate, CandidateBelief) for candidate in candidates
    ):
        raise ProposalWorkflowError(
            ProposalResultCode.candidate_structure_invalid,
            "extractor must return validated CandidateBelief values",
        )

    policy_meta = policy_snapshot(policy)
    proposals: list[Proposal] = []
    for candidate in candidates:
        if candidate.scope != event.scope:
            raise ProposalWorkflowError(
                ProposalResultCode.candidate_scope_denied,
                "candidate scope cannot elevate or diverge from the source event scope",
            )
        same_source = [
            belief
            for belief in store.current_beliefs(candidate.entity, candidate.attribute)
            if belief.source_id == source_id
        ]
        if len(same_source) > 1:
            raise ProposalWorkflowError(
                ProposalResultCode.candidate_structure_invalid,
                "structural key has multiple current same-source beliefs",
            )
        expected = same_source[0].id if same_source else None
        proposal = Proposal(
            id=proposal_id_factory(),
            source_event_id=event.id,
            source_id=source.id,
            source_type=source.type,
            entity=candidate.entity,
            attribute=candidate.attribute,
            value=candidate.value,
            proposed_status=candidate.proposed_status,
            effective_status=clamp_status(
                candidate.proposed_status, source.type, policy
            ),
            scope=candidate.scope,
            decision_type=candidate.decision_type,
            creator_agent_id=agent_id,
            created_at=as_of,
            policy_version=policy_meta.version,
            policy_fingerprint=policy_meta.fingerprint,
            expected_current_belief_id=expected,
            expected_current_absent=expected is None,
            creation_trace_id=creation_trace_id,
            state=ProposalState.pending,
        )
        proposals.append(store.add_proposal(proposal))
    return ProposalCreateResult(
        ok=True,
        code=ProposalResultCode.proposals_created,
        message="candidate beliefs queued for explicit approval",
        event=event,
        proposals=proposals,
        trace_id=creation_trace_id,
    )


def list_proposals(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    approval_actor_id: Optional[str],
    request: ProposalListRequest,
) -> ProposalListResult:
    try:
        active, effective = approval_scopes(
            policy,
            agent_id,
            approval_actor_id,
            request.scope,
            request.task_type,
        )
    except ProposalWorkflowError as exc:
        return ProposalListResult(authorized=False, code=exc.code)
    states = [request.state] if request.state is not None else None
    denied = [scope for scope in active if scope not in effective]
    return ProposalListResult(
        authorized=True,
        code=ProposalResultCode.proposals_listed,
        proposals=store.list_proposals(scopes=effective, states=states),
        exclusions=[
            ProposalExclusionSummary(
                reason_code="task_scope_mismatch",
                rule_id="M7-PROPOSAL-SCOPE-TASK-001",
                count=store.count_proposals(scopes=active, invert_scopes=True),
                safe_detail="proposal outside active task scopes",
            ),
            ProposalExclusionSummary(
                reason_code="agent_scope_denied",
                rule_id="M7-PROPOSAL-SCOPE-AGENT-001",
                count=store.count_proposals(scopes=denied),
                safe_detail="proposal outside agent readable scopes",
            ),
        ],
    )


def materialize_approved_proposal(
    store: Store,
    policy: TrustPolicy,
    proposal: Proposal,
    *,
    as_of: datetime,
) -> tuple[Optional[Belief], Optional[ProposalResultCode]]:
    """Return a belief on success or a terminal stale reason without writing one."""
    event = store.get_event(proposal.source_event_id)
    source = store.get_source(proposal.source_id)
    if (
        event is None
        or source is None
        or event.source_id != proposal.source_id
        or event.scope != proposal.scope
        or source.type != proposal.source_type
    ):
        return None, ProposalResultCode.source_invalid
    current_policy = policy_snapshot(policy)
    if (
        current_policy.version != proposal.policy_version
        or current_policy.fingerprint != proposal.policy_fingerprint
        or clamp_status(proposal.proposed_status, source.type, policy)
        != proposal.effective_status
    ):
        return None, ProposalResultCode.policy_changed
    same_source = [
        belief
        for belief in store.current_beliefs(proposal.entity, proposal.attribute)
        if belief.source_id == proposal.source_id
    ]
    expected = (
        None
        if proposal.expected_current_absent
        else proposal.expected_current_belief_id
    )
    if len(same_source) > 1 or (
        same_source[0].id if same_source else None
    ) != expected:
        return None, ProposalResultCode.structurally_stale

    candidate = CandidateBelief(
        entity=proposal.entity,
        attribute=proposal.attribute,
        value=proposal.value,
        proposed_status=proposal.proposed_status,
        scope=proposal.scope,
        decision_type=proposal.decision_type,
    )
    belief = materialize_candidate(
        store,
        policy,
        source=source,
        event=event,
        candidate=candidate,
        as_of=as_of,
        valid_from=event.created_at,
        expected_current_belief_id=expected,
    )
    if belief.status != proposal.effective_status:
        raise RuntimeError("approved belief status differs from reviewed effective status")
    return belief, None


def execute_proposal_decision(
    store: Store,
    policy: TrustPolicy,
    proposal: Proposal,
    target: ProposalState,
    *,
    trace_id: str,
    session_id: str,
    session_mode: SessionMode,
    agent_id: str,
    approval_actor_id: str,
    active_scope: str,
    task_type: Optional[str],
    as_of: datetime,
) -> ProposalDecisionExecution:
    """Apply one authorized pending decision and return its unpersisted trace."""
    if proposal.state != ProposalState.pending:
        raise ValueError("proposal decision execution requires a pending proposal")
    if target not in {ProposalState.approved, ProposalState.rejected}:
        raise ValueError("proposal decision target must be approved or rejected")

    common = {
        "trace_id": trace_id,
        "session_id": session_id,
        "session_mode": session_mode,
        "agent_id": agent_id,
        "approval_actor_id": approval_actor_id,
        "active_scope": active_scope,
        "task_type": task_type,
        "policy": policy,
        "created_at": as_of,
    }
    if target == ProposalState.rejected:
        trace = build_mutation_trace(
            MutationAuditSnapshot(
                proposal_ids=[proposal.id],
                proposals=[proposal_snapshot(proposal)],
                previous_state=ProposalState.pending.value,
                new_state=ProposalState.rejected.value,
            ),
            operation=AuditOperation.proposal_reject,
            outcome=AuditOutcome.completed,
            result_code=ProposalResultCode.proposal_rejected.value,
            reason_codes=[ProposalResultCode.proposal_rejected.value],
            rule_ids=["M7-PROPOSAL-REJECT-001"],
            **common,
        )
        decided = store.decide_proposal(
            proposal.id,
            state=ProposalState.rejected,
            actor_id=approval_actor_id,
            decided_at=as_of,
            decision_trace_id=trace_id,
            approved_belief_id=None,
            terminal_reason_code=ProposalResultCode.proposal_rejected.value,
        )
        return ProposalDecisionExecution(
            ok=True,
            code=ProposalResultCode.proposal_rejected,
            message="proposal rejected",
            proposal=decided,
            belief=None,
            trace=trace,
        )

    belief, stale_reason = materialize_approved_proposal(
        store, policy, proposal, as_of=as_of
    )
    if stale_reason is not None:
        observed_current_ids = [
            item.id
            for item in store.current_beliefs(proposal.entity, proposal.attribute)
            if item.source_id == proposal.source_id
        ]
        trace = build_mutation_trace(
            MutationAuditSnapshot(
                proposal_ids=[proposal.id],
                proposals=[proposal_snapshot(proposal)],
                observed_current_belief_ids=observed_current_ids,
                previous_state=ProposalState.pending.value,
                new_state=ProposalState.stale.value,
            ),
            operation=AuditOperation.proposal_approve,
            outcome=AuditOutcome.stale,
            result_code=ProposalResultCode.proposal_stale.value,
            reason_codes=[stale_reason.value],
            rule_ids=["M7-PROPOSAL-STALE-001"],
            **common,
        )
        decided = store.decide_proposal(
            proposal.id,
            state=ProposalState.stale,
            actor_id=approval_actor_id,
            decided_at=as_of,
            decision_trace_id=trace_id,
            approved_belief_id=None,
            terminal_reason_code=stale_reason.value,
        )
        return ProposalDecisionExecution(
            ok=False,
            code=ProposalResultCode.proposal_stale,
            message="proposal became stale and committed no belief",
            proposal=decided,
            belief=None,
            trace=trace,
        )

    evidence = snapshots_for_beliefs(store, [belief])
    trace = build_mutation_trace(
        MutationAuditSnapshot(
            belief_ids=[belief.id],
            beliefs=evidence,
            proposal_ids=[proposal.id],
            proposals=[proposal_snapshot(proposal)],
            previous_state=ProposalState.pending.value,
            new_state=ProposalState.approved.value,
        ),
        operation=AuditOperation.proposal_approve,
        outcome=AuditOutcome.completed,
        result_code=ProposalResultCode.proposal_approved.value,
        reason_codes=[ProposalResultCode.proposal_approved.value],
        rule_ids=[
            "M7-PROPOSAL-APPROVE-001",
            "M2-STATUS-CLAMP-001",
            "M1-SAME-SOURCE-SUPERSESSION-001",
        ],
        **common,
    )
    decided = store.decide_proposal(
        proposal.id,
        state=ProposalState.approved,
        actor_id=approval_actor_id,
        decided_at=as_of,
        decision_trace_id=trace_id,
        approved_belief_id=belief.id,
        terminal_reason_code=ProposalResultCode.proposal_approved.value,
    )
    return ProposalDecisionExecution(
        ok=True,
        code=ProposalResultCode.proposal_approved,
        message="proposal approved and belief committed",
        proposal=decided,
        belief=belief,
        trace=trace,
    )
