"""MemoryStore — the project's only public service boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from uuid import uuid4

from .assemble import assemble_context
from .audit import (
    AuditPersistenceError,
    belief_snapshot,
    build_assembly_trace,
    build_gate_trace,
    build_mutation_trace,
    explain as _explain,
    proposal_snapshot,
    snapshots_for_beliefs,
)
from .commitments import (
    add_commitment as _add_commitment,
    list_commitments as _list_commitments,
    surface_overdue as _surface_overdue,
    transition_commitment as _transition_commitment,
)
from .ingest import Extractor, ingest_event, live_extractor
from .models import (
    ArtifactImpactAuditSnapshot,
    ArtifactRegistrationRequest,
    ArtifactRegistrationResult,
    AssembledContext,
    AssemblyRequest,
    AuditOperation,
    AuditOutcome,
    AuditResultCode,
    AuditTrace,
    CommitmentCreateRequest,
    CommitmentListRequest,
    CommitmentListResult,
    CommitmentMutationResult,
    CommitmentResultCode,
    CommitmentTransitionRequest,
    CorrectionRequest,
    CorrectionResult,
    DependencyRegistrationRequest,
    DependencyRegistrationResult,
    ExplainRequest,
    ExplainResult,
    GateDecision,
    GateResult,
    IngestResult,
    IngestResultCode,
    M6ResultCode,
    MutationAuditSnapshot,
    OverdueScanRequest,
    OverdueScanResult,
    PolicyReason,
    ProposalCreateResult,
    ProposalDecisionRequest,
    ProposalDecisionResult,
    ProposalListRequest,
    ProposalListResult,
    ProposalResultCode,
    ProposalState,
    RetrievalRequest,
    RetrievalResult,
    SessionMode,
    Scope,
    TrustPolicy,
)
from .policy import gate as _gate
from .propagate import (
    correct_belief as _correct_belief,
    register_artifact as _register_artifact,
    register_dependency as _register_dependency,
)
from .proposals import (
    ProposalWorkflowError,
    approval_scopes,
    create_proposals,
    execute_proposal_decision,
    list_proposals as _list_proposals,
    propose_agent,
)
from .retrieve import retrieve_beliefs
from .store import Store


Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]


def _utc_now() -> datetime:
    """The sole accepted default wall-clock boundary."""
    return datetime.now(timezone.utc)


def _new_id(kind: str) -> str:
    """The sole accepted default randomness boundary for trusted host IDs."""
    return f"{kind}-{uuid4().hex}"


def _validate_clock_result(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("MemoryStore clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("MemoryStore clock must return timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("MemoryStore clock must return timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


class MemoryStore:
    def __init__(
        self,
        db_path: str,
        policy: Optional[TrustPolicy] = None,
        *,
        agent_id: str,
        session_mode: SessionMode = SessionMode.direct,
        session_id: Optional[str] = None,
        approval_actor_id: Optional[str] = None,
        live: bool = False,
        clock: Optional[Clock] = None,
        id_factory: Optional[IdFactory] = None,
    ):
        self._agent_id = _required_text(agent_id, "agent_id")
        self._session_mode = SessionMode(session_mode)
        self._approval_actor_id = (
            _required_text(approval_actor_id, "approval_actor_id")
            if approval_actor_id is not None
            else None
        )
        self._id_factory = id_factory if id_factory is not None else _new_id
        generated_session = session_id or self._make_id("session")
        self._session_id = _required_text(generated_session, "session_id")
        self._store = Store(
            db_path, read_only=self._session_mode == SessionMode.ephemeral
        )
        self.policy = policy
        self.live = live
        self._clock = clock if clock is not None else _utc_now
        self._transient_traces: dict[str, AuditTrace] = {}

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def session_mode(self) -> SessionMode:
        return self._session_mode

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def approval_actor_id(self) -> Optional[str]:
        return self._approval_actor_id

    def _make_id(self, kind: str) -> str:
        return _required_text(self._id_factory(kind), f"{kind} id")

    def _authoritative_now(self) -> datetime:
        return _validate_clock_result(self._clock())

    def _require_policy(self, operation: str) -> TrustPolicy:
        if self.policy is None:
            raise ValueError(
                f"MemoryStore requires a policy to {operation} "
                "(pass policy=load_policy(...))"
            )
        return self.policy

    def _required_audit_insert(self, trace: AuditTrace) -> AuditTrace:
        try:
            return self._store.add_audit_trace(trace)
        except Exception as exc:
            raise AuditPersistenceError("required audit trace could not be persisted") from exc

    def _record_response_trace(self, trace: AuditTrace) -> None:
        if self._session_mode == SessionMode.ephemeral:
            if (
                trace.trace_id in self._transient_traces
                or self._store.get_audit_trace(trace.trace_id) is not None
            ):
                raise AuditPersistenceError("transient trace ID collision")
            self._transient_traces[trace.trace_id] = trace.model_copy(deep=True)
            return
        with self._store.transaction():
            self._required_audit_insert(trace)

    def close(self) -> None:
        """Close this local session. Lifecycle-only; deliberately untraced."""
        self._transient_traces.clear()
        self._store.close()

    def ingest(
        self,
        *,
        source_id: str,
        content: str,
        scope: str,
        meta: Optional[dict] = None,
        extractor: Optional[Extractor] = None,
    ) -> IngestResult | ProposalCreateResult:
        if self._session_mode == SessionMode.ephemeral:
            return IngestResult(
                ok=False,
                code=IngestResultCode.ephemeral_write_blocked,
                event=None,
                beliefs=[],
            )
        policy = self._require_policy("ingest")
        if extractor is None:
            if not self.live:
                raise ValueError(
                    "no extractor supplied and live mode is off — pass extractor=... "
                    "(a fixture) or construct MemoryStore(..., live=True) with "
                    "ANTHROPIC_API_KEY set"
                )
            extractor = live_extractor

        if self._session_mode == SessionMode.propose:
            try:
                propose_agent(policy, self._agent_id)
            except ProposalWorkflowError as exc:
                return ProposalCreateResult(
                    ok=False, code=exc.code, message=str(exc)
                )
            if scope is None:
                return ProposalCreateResult(
                    ok=False,
                    code=ProposalResultCode.scope_context_missing,
                    message="explicit task scope is required",
                )
            try:
                Scope.parse(scope)
            except (TypeError, ValueError):
                return ProposalCreateResult(
                    ok=False,
                    code=ProposalResultCode.candidate_structure_invalid,
                    message="proposal scope is structurally invalid",
                )
            as_of = self._authoritative_now()
            trace_id = self._make_id("trace")
            try:
                with self._store.transaction(immediate=True):
                    result = create_proposals(
                        self._store,
                        policy,
                        self._agent_id,
                        source_id=source_id,
                        content=content,
                        scope=scope,
                        meta=meta,
                        extractor=extractor,
                        as_of=as_of,
                        creation_trace_id=trace_id,
                        proposal_id_factory=lambda: self._make_id("proposal"),
                    )
                    trace = build_mutation_trace(
                        MutationAuditSnapshot(
                            event_ids=[result.event.id] if result.event else [],
                            proposal_ids=[proposal.id for proposal in result.proposals],
                            proposals=[
                                proposal_snapshot(proposal)
                                for proposal in result.proposals
                            ],
                            previous_state=None,
                            new_state=ProposalState.pending.value,
                        ),
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        approval_actor_id=None,
                        active_scope=scope,
                        task_type=None,
                        operation=AuditOperation.proposal_create,
                        outcome=AuditOutcome.completed,
                        result_code=ProposalResultCode.proposals_created.value,
                        reason_codes=[ProposalResultCode.proposals_created.value],
                        rule_ids=["M7-PROPOSAL-CREATE-001", "M2-STATUS-CLAMP-001"],
                        policy=policy,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
            except ProposalWorkflowError as exc:
                return ProposalCreateResult(
                    ok=False, code=exc.code, message=str(exc)
                )
            except AuditPersistenceError:
                return ProposalCreateResult(
                    ok=False,
                    code=ProposalResultCode.audit_persistence_failed,
                    message="event and proposals rolled back because audit persistence failed",
                )
            except Exception:
                return ProposalCreateResult(
                    ok=False,
                    code=ProposalResultCode.atomic_operation_failed,
                    message="proposal creation rolled back atomically",
                )
            return result

        as_of = self._authoritative_now()
        trace_id = self._make_id("trace")
        try:
            with self._store.transaction(immediate=True):
                result = ingest_event(
                    self._store,
                    policy,
                    source_id=source_id,
                    content=content,
                    scope=scope,
                    meta=meta,
                    extractor=extractor,
                    as_of=as_of,
                )
                evidence = snapshots_for_beliefs(self._store, result.beliefs)
                trace = build_mutation_trace(
                    MutationAuditSnapshot(
                        event_ids=[result.event.id] if result.event else [],
                        belief_ids=[belief.id for belief in result.beliefs],
                        beliefs=evidence,
                    ),
                    trace_id=trace_id,
                    session_id=self._session_id,
                    session_mode=self._session_mode,
                    agent_id=self._agent_id,
                    approval_actor_id=None,
                    active_scope=scope,
                    task_type=None,
                    operation=AuditOperation.ingest,
                    outcome=AuditOutcome.completed,
                    result_code=IngestResultCode.beliefs_committed.value,
                    reason_codes=[IngestResultCode.beliefs_committed.value],
                    rule_ids=[
                        "M7-DIRECT-INGEST-AUDIT-001",
                        "M2-STATUS-CLAMP-001",
                        "M1-SAME-SOURCE-SUPERSESSION-001",
                    ],
                    policy=policy,
                    created_at=as_of,
                )
                self._required_audit_insert(trace)
        except AuditPersistenceError:
            return IngestResult(
                ok=False,
                code=IngestResultCode.audit_persistence_failed,
                event=None,
                beliefs=[],
            )
        return result.model_copy(update={"trace_id": trace_id})

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        policy = self._require_policy("retrieve")
        return retrieve_beliefs(self._store, policy, self._agent_id, request)

    def assemble(self, request: AssemblyRequest) -> AssembledContext:
        policy = self._require_policy("assemble")
        trace_id = self._make_id("trace")
        persisted = self._session_mode != SessionMode.ephemeral
        evaluation_completed = False
        try:
            if not persisted:
                result = assemble_context(self._store, policy, self._agent_id, request)
                evaluation_completed = True
                trace = build_assembly_trace(
                    result,
                    trace_id=trace_id,
                    session_id=self._session_id,
                    session_mode=self._session_mode,
                    agent_id=self._agent_id,
                    policy=policy,
                    persisted=False,
                    created_at=self._authoritative_now(),
                )
                self._record_response_trace(trace)
            else:
                with self._store.transaction(immediate=True):
                    as_of = self._authoritative_now()
                    result = assemble_context(
                        self._store, policy, self._agent_id, request
                    )
                    evaluation_completed = True
                    trace = build_assembly_trace(
                        result,
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        policy=policy,
                        persisted=True,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
        except Exception:
            if not evaluation_completed:
                raise
            return AssembledContext(
                request=request,
                ok=False,
                result_code=AuditResultCode.audit_persistence_failed,
                token_budget=request.token_budget,
            )
        return result.model_copy(update={
            "trace_id": trace_id,
            "trace_persisted": persisted,
        })

    def gate(
        self,
        *,
        action: str,
        entity: str,
        scope: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> GateResult:
        policy = self._require_policy("gate")
        action_spec = policy.actions.get(action)
        if scope is None:
            if action_spec is None:
                return _gate(
                    action, [], policy, {}, agent_id=self._agent_id
                ).model_copy(update={"ok": False})
            detail = PolicyReason(
                code="scope_context_missing",
                rule_id="SCOPE-CONTEXT-REQUIRED",
                message="active task scope is required for public gate evaluation",
            )
            return GateResult(
                ok=False,
                decision=GateDecision.deny,
                action=action,
                decision_type=action_spec.decision,
                risk_tier=action_spec.risk,
                rule_ids=[detail.rule_id],
                reason_codes=[detail.code],
                reasons=[detail.message],
                details=[detail],
            )
        trace_id = self._make_id("trace")
        persisted = self._session_mode != SessionMode.ephemeral

        def evaluate():
            if action_spec is None:
                return (
                    _gate(action, [], policy, {}, agent_id=self._agent_id),
                    [],
                    [],
                )
            retrieval = retrieve_beliefs(
                self._store,
                policy,
                self._agent_id,
                RetrievalRequest(
                    entity=entity,
                    attribute=action_spec.decision,
                    scope=scope,
                    task_type=task_type,
                ),
            )
            items = retrieval.items
            return (
                _gate(
                    action,
                    [item.belief for item in items],
                    policy,
                    {item.source.id: item.source.type for item in items},
                    agent_id=self._agent_id,
                ),
                items,
                retrieval.exclusions,
            )

        try:
            if not persisted:
                result, items, exclusions = evaluate()
                trace = build_gate_trace(
                    result,
                    items,
                    exclusions,
                    entity=entity,
                    scope=scope,
                    task_type=task_type,
                    trace_id=trace_id,
                    session_id=self._session_id,
                    session_mode=self._session_mode,
                    agent_id=self._agent_id,
                    policy=policy,
                    persisted=False,
                    created_at=self._authoritative_now(),
                )
                self._record_response_trace(trace)
            else:
                with self._store.transaction(immediate=True):
                    as_of = self._authoritative_now()
                    result, items, exclusions = evaluate()
                    trace = build_gate_trace(
                        result,
                        items,
                        exclusions,
                        entity=entity,
                        scope=scope,
                        task_type=task_type,
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        policy=policy,
                        persisted=True,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
        except Exception:
            detail = PolicyReason(
                code="audit_persistence_failed",
                rule_id="M7-AUDIT-REQUIRED-001",
                message="gate failed closed because its required audit trace was unavailable",
            )
            return GateResult(
                ok=False,
                result_code=AuditResultCode.audit_persistence_failed,
                decision=GateDecision.deny,
                action=action,
                decision_type=action_spec.decision if action_spec else None,
                risk_tier=action_spec.risk if action_spec else None,
                rule_ids=[detail.rule_id],
                reason_codes=[detail.code],
                reasons=[detail.message],
                details=[detail],
            )
        return result.model_copy(update={
            "trace_id": trace_id,
            "trace_persisted": persisted,
        })

    def register_artifact(
        self, request: ArtifactRegistrationRequest
    ) -> ArtifactRegistrationResult:
        if self._session_mode == SessionMode.ephemeral:
            return ArtifactRegistrationResult(
                ok=False,
                code=M6ResultCode.ephemeral_write_blocked,
                message="ephemeral sessions cannot register artifacts",
            )
        policy = self._require_policy("register artifacts")
        as_of = self._authoritative_now()
        trace_id = self._make_id("trace")
        try:
            with self._store.transaction(immediate=True):
                result = _register_artifact(
                    self._store, policy, self._agent_id, request, as_of=as_of
                )
                if result.ok:
                    trace = build_mutation_trace(
                        MutationAuditSnapshot(artifact_ids=[result.artifact.id]),
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        approval_actor_id=None,
                        active_scope=request.scope,
                        task_type=None,
                        operation=AuditOperation.artifact_register,
                        outcome=AuditOutcome.completed,
                        result_code=result.code.value,
                        reason_codes=[result.code.value],
                        rule_ids=["M6-ARTIFACT-REGISTER-001"],
                        policy=policy,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
        except AuditPersistenceError:
            return ArtifactRegistrationResult(
                ok=False,
                code=M6ResultCode.audit_persistence_failed,
                message="artifact registration rolled back because audit persistence failed",
            )
        return result.model_copy(update={"trace_id": trace_id}) if result.ok else result

    def register_dependency(
        self, request: DependencyRegistrationRequest
    ) -> DependencyRegistrationResult:
        if self._session_mode == SessionMode.ephemeral:
            return DependencyRegistrationResult(
                ok=False,
                code=M6ResultCode.ephemeral_write_blocked,
                message="ephemeral sessions cannot register dependencies",
            )
        policy = self._require_policy("register dependencies")
        as_of = self._authoritative_now()
        trace_id = self._make_id("trace")
        try:
            with self._store.transaction(immediate=True):
                result = _register_dependency(
                    self._store, policy, self._agent_id, request, as_of=as_of
                )
                if result.ok:
                    trace = build_mutation_trace(
                        MutationAuditSnapshot(
                            dependency_ids=[result.dependency.id],
                            artifact_ids=[request.downstream_artifact_id],
                            belief_ids=(
                                [request.upstream_id]
                                if request.upstream_kind.value == "belief"
                                else []
                            ),
                        ),
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        approval_actor_id=None,
                        active_scope=request.scope,
                        task_type=request.task_type,
                        operation=AuditOperation.dependency_register,
                        outcome=AuditOutcome.completed,
                        result_code=result.code.value,
                        reason_codes=[result.code.value],
                        rule_ids=["M6-DEPENDENCY-REGISTER-001"],
                        policy=policy,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
        except AuditPersistenceError:
            return DependencyRegistrationResult(
                ok=False,
                code=M6ResultCode.audit_persistence_failed,
                message="dependency registration rolled back because audit persistence failed",
            )
        return result.model_copy(update={"trace_id": trace_id}) if result.ok else result

    def correct(self, request: CorrectionRequest) -> CorrectionResult:
        if self._session_mode == SessionMode.ephemeral:
            return CorrectionResult(
                ok=False,
                code=M6ResultCode.ephemeral_write_blocked,
                message="ephemeral sessions cannot correct beliefs",
                as_of=None,
            )
        policy = self._require_policy("correct beliefs")
        as_of = self._authoritative_now()
        trace_id = self._make_id("trace")
        target = self._store.get_belief(request.belief_id)
        target_snapshot = (
            snapshots_for_beliefs(self._store, [target])[0]
            if target is not None
            else None
        )

        def audit_callback(result: CorrectionResult) -> None:
            try:
                hidden_count = sum(item.count for item in result.hidden_impacts)
                trace = build_mutation_trace(
                    MutationAuditSnapshot(
                        event_ids=[result.event.id] if result.event else [],
                        belief_ids=[result.belief.id] if result.belief else [],
                        beliefs=(
                            snapshots_for_beliefs(self._store, [result.belief])
                            if result.belief
                            else []
                        ),
                        target_belief=target_snapshot,
                        visible_impact_artifact_ids=[
                            impact.artifact.id for impact in result.visible_impacts
                        ],
                        visible_impacts=[ArtifactImpactAuditSnapshot(
                            artifact_id=impact.artifact.id,
                            depth=impact.depth,
                            previous_state=impact.previous_state,
                            new_state=impact.artifact.propagation_state,
                            reason_code=impact.reason_code,
                            rule_id=impact.rule_id,
                            state_changed=impact.state_changed,
                        ) for impact in result.visible_impacts],
                        hidden_impact_count=hidden_count,
                        affected_count=result.affected_count,
                    ),
                    trace_id=trace_id,
                    session_id=self._session_id,
                    session_mode=self._session_mode,
                    agent_id=self._agent_id,
                    approval_actor_id=None,
                    active_scope=request.scope,
                    task_type=request.task_type,
                    operation=AuditOperation.correction,
                    outcome=AuditOutcome.completed,
                    result_code=result.code.value,
                    reason_codes=[
                        result.code.value,
                        *(impact.reason_code.value for impact in result.visible_impacts),
                        *(impact.reason_code.value for impact in result.hidden_impacts),
                    ],
                    rule_ids=[
                        "M6-CORRECTION-AUTHORITY-001",
                        "M1-SAME-SOURCE-SUPERSESSION-001",
                        *(impact.rule_id for impact in result.visible_impacts),
                        *(impact.rule_id for impact in result.hidden_impacts),
                    ],
                    policy=policy,
                    created_at=as_of,
                )
                self._required_audit_insert(trace)
            except AuditPersistenceError:
                raise
            except Exception as exc:
                raise AuditPersistenceError("correction audit construction failed") from exc

        result = _correct_belief(
            self._store,
            policy,
            self._agent_id,
            request,
            as_of=as_of,
            audit_callback=audit_callback,
        )
        return result.model_copy(update={"trace_id": trace_id}) if result.ok else result

    def explain(self, request: ExplainRequest) -> ExplainResult:
        policy = self._require_policy("explain")
        return _explain(
            self._store,
            policy,
            self._agent_id,
            request,
            self._transient_traces,
        )

    def add_commitment(
        self, request: CommitmentCreateRequest
    ) -> CommitmentMutationResult:
        if self._session_mode == SessionMode.ephemeral:
            return CommitmentMutationResult(
                ok=False,
                code=CommitmentResultCode.ephemeral_write_blocked,
                message="ephemeral sessions cannot add commitments",
            )
        policy = self._require_policy("add commitments")
        as_of = self._authoritative_now()
        trace_id = self._make_id("trace")
        try:
            with self._store.transaction(immediate=True):
                result = _add_commitment(
                    self._store, policy, self._agent_id, request, as_of=as_of
                )
                if result.ok:
                    trace = build_mutation_trace(
                        MutationAuditSnapshot(
                            commitment_ids=[result.commitment.id],
                            previous_state=None,
                            new_state=result.commitment.state.value,
                        ),
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        approval_actor_id=None,
                        active_scope=request.scope,
                        task_type=None,
                        operation=AuditOperation.commitment_create,
                        outcome=AuditOutcome.completed,
                        result_code=result.code.value,
                        reason_codes=[result.code.value],
                        rule_ids=["M5-COMMITMENT-CREATE-001"],
                        policy=policy,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
        except AuditPersistenceError:
            return CommitmentMutationResult(
                ok=False,
                code=CommitmentResultCode.audit_persistence_failed,
                message="commitment creation rolled back because audit persistence failed",
            )
        return result.model_copy(update={"trace_id": trace_id}) if result.ok else result

    def transition_commitment(
        self, request: CommitmentTransitionRequest
    ) -> CommitmentMutationResult:
        if self._session_mode == SessionMode.ephemeral:
            return CommitmentMutationResult(
                ok=False,
                code=CommitmentResultCode.ephemeral_write_blocked,
                message="ephemeral sessions cannot transition commitments",
            )
        policy = self._require_policy("transition commitments")
        as_of = self._authoritative_now()
        trace_id = self._make_id("trace")
        try:
            with self._store.transaction(immediate=True):
                before = self._store.get_commitment(request.commitment_id)
                result = _transition_commitment(
                    self._store, policy, self._agent_id, request, as_of=as_of
                )
                if result.ok:
                    trace = build_mutation_trace(
                        MutationAuditSnapshot(
                            commitment_ids=[result.commitment.id],
                            previous_state=before.state.value if before else None,
                            new_state=result.commitment.state.value,
                        ),
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        approval_actor_id=None,
                        active_scope=request.scope,
                        task_type=request.task_type,
                        operation=AuditOperation.commitment_transition,
                        outcome=AuditOutcome.completed,
                        result_code=result.code.value,
                        reason_codes=[result.code.value],
                        rule_ids=["M5-COMMITMENT-TRANSITION-001"],
                        policy=policy,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
        except AuditPersistenceError:
            return CommitmentMutationResult(
                ok=False,
                code=CommitmentResultCode.audit_persistence_failed,
                message="commitment transition rolled back because audit persistence failed",
            )
        return result.model_copy(update={"trace_id": trace_id}) if result.ok else result

    def list_commitments(
        self, request: CommitmentListRequest
    ) -> CommitmentListResult:
        policy = self._require_policy("list commitments")
        return _list_commitments(self._store, policy, self._agent_id, request)

    def surface_overdue(self, request: OverdueScanRequest) -> OverdueScanResult:
        if self._session_mode == SessionMode.ephemeral:
            return OverdueScanResult(
                authorized=False,
                code=CommitmentResultCode.ephemeral_write_blocked,
                as_of=None,
                overdue=[],
                promoted_count=0,
                exclusions=[],
            )
        policy = self._require_policy("scan overdue commitments")
        as_of = self._authoritative_now()
        trace_id = self._make_id("trace")
        try:
            with self._store.transaction(immediate=True):
                before = {
                    item.id: item.state for item in self._store.list_commitments()
                }
                result = _surface_overdue(
                    self._store, policy, self._agent_id, request, as_of=as_of
                )
                promoted = [
                    item.id
                    for item in result.overdue
                    if before.get(item.id) is not None
                    and before[item.id].value != item.state.value
                ]
                if result.authorized and promoted:
                    trace = build_mutation_trace(
                        MutationAuditSnapshot(
                            commitment_ids=promoted,
                            previous_state="open_or_waiting",
                            new_state="overdue",
                            promoted_commitment_ids=promoted,
                        ),
                        trace_id=trace_id,
                        session_id=self._session_id,
                        session_mode=self._session_mode,
                        agent_id=self._agent_id,
                        approval_actor_id=None,
                        active_scope=request.scope,
                        task_type=request.task_type,
                        operation=AuditOperation.overdue_scan,
                        outcome=AuditOutcome.completed,
                        result_code=result.code.value,
                        reason_codes=[result.code.value],
                        rule_ids=["M5-OVERDUE-SCAN-001"],
                        policy=policy,
                        created_at=as_of,
                    )
                    self._required_audit_insert(trace)
                    result = result.model_copy(update={"trace_id": trace_id})
        except AuditPersistenceError:
            return OverdueScanResult(
                authorized=False,
                code=CommitmentResultCode.audit_persistence_failed,
                as_of=as_of,
                overdue=[],
                promoted_count=0,
                exclusions=[],
            )
        return result

    def list_proposals(self, request: ProposalListRequest) -> ProposalListResult:
        policy = self._require_policy("list proposals")
        return _list_proposals(
            self._store,
            policy,
            self._agent_id,
            self._approval_actor_id,
            request,
        )

    def approve_proposal(
        self, request: ProposalDecisionRequest
    ) -> ProposalDecisionResult:
        return self._decide_proposal(request, ProposalState.approved)

    def reject_proposal(
        self, request: ProposalDecisionRequest
    ) -> ProposalDecisionResult:
        return self._decide_proposal(request, ProposalState.rejected)

    def _decide_proposal(
        self, request: ProposalDecisionRequest, target: ProposalState
    ) -> ProposalDecisionResult:
        if self._session_mode == SessionMode.ephemeral:
            return ProposalDecisionResult(
                ok=False,
                code=ProposalResultCode.ephemeral_write_blocked,
                message="ephemeral sessions cannot decide proposals",
            )
        policy = self._require_policy("decide proposals")
        try:
            approval_scopes(
                policy,
                self._agent_id,
                self._approval_actor_id,
                request.scope,
                request.task_type,
            )
        except ProposalWorkflowError as exc:
            return ProposalDecisionResult(ok=False, code=exc.code, message=str(exc))

        try:
            with self._store.transaction(immediate=True):
                _, effective = approval_scopes(
                    policy,
                    self._agent_id,
                    self._approval_actor_id,
                    request.scope,
                    request.task_type,
                )
                proposal = self._store.get_proposal(
                    request.proposal_id, scopes=effective
                )
                if proposal is None:
                    return ProposalDecisionResult(
                        ok=False,
                        code=ProposalResultCode.proposal_unavailable,
                        message="proposal is unavailable",
                    )
                if self._approval_actor_id == proposal.creator_agent_id:
                    return ProposalDecisionResult(
                        ok=False,
                        code=ProposalResultCode.approval_actor_not_distinct,
                        message="proposal creator cannot act as human approver",
                    )
                if proposal.state != ProposalState.pending:
                    belief = (
                        self._store.get_belief(proposal.approved_belief_id)
                        if proposal.approved_belief_id is not None
                        else None
                    )
                    return ProposalDecisionResult(
                        ok=proposal.state == target,
                        code=ProposalResultCode.proposal_already_decided,
                        message="proposal already has a terminal decision",
                        proposal=proposal,
                        belief=belief,
                        trace_id=proposal.decision_trace_id,
                    )

                as_of = self._authoritative_now()
                trace_id = self._make_id("trace")
                execution = execute_proposal_decision(
                    self._store,
                    policy,
                    proposal,
                    target,
                    trace_id=trace_id,
                    session_id=self._session_id,
                    session_mode=self._session_mode,
                    agent_id=self._agent_id,
                    approval_actor_id=self._approval_actor_id,
                    active_scope=request.scope,
                    task_type=request.task_type,
                    as_of=as_of,
                )
                self._required_audit_insert(execution.trace)
                return ProposalDecisionResult(
                    ok=execution.ok,
                    code=execution.code,
                    message=execution.message,
                    proposal=execution.proposal,
                    belief=execution.belief,
                    trace_id=trace_id,
                )
        except AuditPersistenceError:
            return ProposalDecisionResult(
                ok=False,
                code=ProposalResultCode.audit_persistence_failed,
                message="proposal decision rolled back because audit persistence failed",
            )
        except Exception:
            return ProposalDecisionResult(
                ok=False,
                code=ProposalResultCode.atomic_operation_failed,
                message="proposal decision rolled back atomically",
            )
