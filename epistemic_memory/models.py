"""Pydantic v2 models for Epistemic Memory. See PLAN.md §2 for rationale."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EpistemicStatus(str, Enum):
    mentioned = "mentioned"
    user_stated = "user_stated"
    third_party_stated = "third_party_stated"
    ai_inferred = "ai_inferred"
    considering = "considering"
    planned = "planned"
    promised = "promised"
    corroborated = "corroborated"
    system_verified = "system_verified"
    disputed = "disputed"
    superseded = "superseded"
    retracted = "retracted"
    do_not_use = "do_not_use"


class RiskTier(str, Enum):
    informational = "informational"
    low_stakes = "low_stakes"
    high_stakes = "high_stakes"
    irreversible = "irreversible"


class GateDecision(str, Enum):
    allow = "allow"
    deny = "deny"
    needs_human = "needs_human"


class SessionMode(str, Enum):
    direct = "direct"
    propose = "propose"
    ephemeral = "ephemeral"


class AuditOperation(str, Enum):
    ingest = "ingest"
    proposal_create = "proposal_create"
    proposal_approve = "proposal_approve"
    proposal_reject = "proposal_reject"
    assemble = "assemble"
    gate = "gate"
    commitment_create = "commitment_create"
    commitment_transition = "commitment_transition"
    overdue_scan = "overdue_scan"
    artifact_register = "artifact_register"
    dependency_register = "dependency_register"
    correction = "correction"


class AuditOutcome(str, Enum):
    completed = "completed"
    denied = "denied"
    stale = "stale"


class AuditResultCode(str, Enum):
    context_assembled = "context_assembled"
    gate_evaluated = "gate_evaluated"
    audit_persistence_failed = "audit_persistence_failed"


class IngestResultCode(str, Enum):
    beliefs_committed = "beliefs_committed"
    audit_persistence_failed = "audit_persistence_failed"
    ephemeral_write_blocked = "ephemeral_write_blocked"


class ProposalState(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    stale = "stale"


class ProposalResultCode(str, Enum):
    proposals_created = "proposals_created"
    proposals_listed = "proposals_listed"
    proposal_approved = "proposal_approved"
    proposal_rejected = "proposal_rejected"
    proposal_stale = "proposal_stale"
    proposal_already_decided = "proposal_already_decided"
    agent_unknown = "agent_unknown"
    operation_not_permitted = "operation_not_permitted"
    approval_actor_required = "approval_actor_required"
    approval_actor_not_distinct = "approval_actor_not_distinct"
    scope_context_missing = "scope_context_missing"
    scope_denied = "scope_denied"
    proposal_unavailable = "proposal_unavailable"
    candidate_scope_denied = "candidate_scope_denied"
    candidate_structure_invalid = "candidate_structure_invalid"
    source_invalid = "source_invalid"
    policy_changed = "policy_changed"
    structurally_stale = "structurally_stale"
    audit_persistence_failed = "audit_persistence_failed"
    atomic_operation_failed = "atomic_operation_failed"
    ephemeral_write_blocked = "ephemeral_write_blocked"


class ExplainResultCode(str, Enum):
    explained = "explained"
    trace_unavailable = "trace_unavailable"
    scope_context_missing = "scope_context_missing"


class CounterfactualCode(str, Enum):
    changed = "changed"
    no_change = "no_change"
    belief_not_in_trace = "belief_not_in_trace"
    counterfactual_not_applicable = "counterfactual_not_applicable"


class StructuralFollowUpCode(str, Enum):
    still_structurally_current = "still_structurally_current"
    superseded_by_later_same_source_belief = "superseded_by_later_same_source_belief"
    current_retraction = "current_retraction"
    superseded_by_retraction = "superseded_by_retraction"
    current_do_not_use = "current_do_not_use"
    current_disputed = "current_disputed"
    current_superseded_status = "current_superseded_status"
    follow_up_unavailable = "follow_up_unavailable"


class CommitmentState(str, Enum):
    open = "open"
    waiting = "waiting"
    fulfilled = "fulfilled"
    cancelled = "cancelled"
    overdue = "overdue"


class CommitmentOperation(str, Enum):
    create = "create"
    transition = "transition"
    scan_overdue = "scan_overdue"


class MemoryOperation(str, Enum):
    correct = "correct"
    register_artifact = "register_artifact"
    register_dependency = "register_dependency"
    explain = "explain"
    propose = "propose"


class ArtifactKind(str, Enum):
    output = "output"
    action = "action"


class ArtifactExecutionState(str, Enum):
    not_applicable = "not_applicable"
    pending = "pending"
    executed = "executed"


class ArtifactPropagationState(str, Enum):
    current = "current"
    stale = "stale"
    halted = "halted"
    review_required = "review_required"


class DependencyEndpointKind(str, Enum):
    belief = "belief"
    artifact = "artifact"


class CorrectionKind(str, Enum):
    correction = "correction"
    retraction = "retraction"


class M6ResultCode(str, Enum):
    artifact_registered = "artifact_registered"
    dependency_registered = "dependency_registered"
    agent_unknown = "agent_unknown"
    operation_not_permitted = "operation_not_permitted"
    scope_context_missing = "scope_context_missing"
    scope_denied = "scope_denied"
    target_belief_not_found = "target_belief_not_found"
    target_belief_not_current = "target_belief_not_current"
    source_mismatch = "source_mismatch"
    source_write_not_permitted = "source_write_not_permitted"
    invalid_correction = "invalid_correction"
    artifact_not_found = "artifact_not_found"
    dependency_endpoint_not_found = "dependency_endpoint_not_found"
    dependency_belief_invalid_state = "dependency_belief_invalid_state"
    dependency_belief_invalid_provenance = "dependency_belief_invalid_provenance"
    dependency_artifact_upstream_invalid_state = (
        "dependency_artifact_upstream_invalid_state"
    )
    dependency_artifact_downstream_invalid_state = (
        "dependency_artifact_downstream_invalid_state"
    )
    dependency_scope_incompatible = "dependency_scope_incompatible"
    dependency_self_edge = "dependency_self_edge"
    dependency_duplicate = "dependency_duplicate"
    dependency_cycle = "dependency_cycle"
    correction_applied = "correction_applied"
    artifact_marked_stale = "artifact_marked_stale"
    pending_action_halted = "pending_action_halted"
    executed_action_requires_review = "executed_action_requires_review"
    hidden_downstream_impacts = "hidden_downstream_impacts"
    atomic_propagation_failure = "atomic_propagation_failure"
    audit_persistence_failed = "audit_persistence_failed"
    ephemeral_write_blocked = "ephemeral_write_blocked"


class CommitmentResultCode(str, Enum):
    commitment_created = "commitment_created"
    commitment_transitioned = "commitment_transitioned"
    commitments_listed = "commitments_listed"
    overdue_scan_completed = "overdue_scan_completed"
    agent_unknown = "agent_unknown"
    operation_not_permitted = "operation_not_permitted"
    managing_agent_required = "managing_agent_required"
    scope_context_missing = "scope_context_missing"
    scope_denied = "scope_denied"
    commitment_not_found = "commitment_not_found"
    state_unknown = "state_unknown"
    transition_invalid = "transition_invalid"
    overdue_scan_required = "overdue_scan_required"
    precondition_unsatisfied = "precondition_unsatisfied"
    proof_required = "proof_required"
    proof_reference_invalid = "proof_reference_invalid"
    proof_not_applicable = "proof_not_applicable"
    deadline_invalid = "deadline_invalid"
    audit_persistence_failed = "audit_persistence_failed"
    ephemeral_write_blocked = "ephemeral_write_blocked"


_SCOPE_RE = re.compile(
    r"^(global|persona)$|^(project|task_type):([A-Za-z0-9][A-Za-z0-9._/-]{0,127})$"
)


class Scope(BaseModel):
    """Structural parse of a scope string: 'global'|'project:<id>'|'persona'|'task_type:<t>'."""

    kind: str
    ref: Optional[str] = None

    @classmethod
    def parse(cls, raw: str) -> "Scope":
        m = _SCOPE_RE.match(raw)
        if not m:
            raise ValueError(f"invalid scope: {raw!r}")
        if m.group(1):
            return cls(kind=m.group(1), ref=None)
        return cls(kind=m.group(2), ref=m.group(3))

    def render(self) -> str:
        return self.kind if self.ref is None else f"{self.kind}:{self.ref}"


def _nonempty(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _scope(value: str) -> str:
    return Scope.parse(value).render()


class Source(BaseModel):
    id: str
    type: str
    label: str
    created_at: str

    @field_validator("id", "type", "label")
    @classmethod
    def validate_nonempty(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)


class Event(BaseModel):
    id: Optional[int] = None
    source_id: str
    content: str
    scope: str
    meta: Optional[dict] = None
    created_at: str

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _nonempty(value, "source_id")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return _scope(value)


class Belief(BaseModel):
    id: Optional[int] = None
    entity: str
    attribute: str
    value: str
    status: EpistemicStatus
    scope: str
    source_id: str
    event_id: Optional[int] = None
    supersedes_id: Optional[int] = None
    decision_type: Optional[str] = None
    valid_from: str
    created_at: str
    # Structural currentness is derived by Store. ``None`` means a caller has
    # not supplied that derivation, so the pure gate must fail closed.
    is_current: Optional[bool] = None

    @field_validator("entity", "attribute", "value", "source_id")
    @classmethod
    def validate_nonempty(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return _scope(value)

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity, self.attribute)


class CandidateBelief(BaseModel):
    """What the LLM proposes from an event. Never committed as-is (spec principle 2)."""

    model_config = ConfigDict(extra="forbid")

    entity: str
    attribute: str
    value: str
    proposed_status: EpistemicStatus
    scope: str
    decision_type: Optional[str] = None

    @field_validator("entity", "attribute", "value")
    @classmethod
    def validate_nonempty(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return _scope(value)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _commitment_text(value: str, field_name: str, maximum: int) -> str:
    _nonempty(value, field_name)
    if len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return value


class CommitmentPrecondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    belief_id: int = Field(gt=0)
    require_uncontradicted: bool = False


class _CommitmentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    owner: str
    beneficiary: str
    scope: Optional[str]
    deadline: datetime
    preconditions: list[CommitmentPrecondition] = Field(default_factory=list)
    proof_required: bool = False

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _commitment_text(value, "description", 4096)

    @field_validator("owner", "beneficiary")
    @classmethod
    def validate_party(cls, value: str, info) -> str:
        return _commitment_text(value, info.field_name, 256)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("deadline")
    @classmethod
    def validate_timestamp(cls, value: datetime, info) -> datetime:
        return _aware_utc(value, info.field_name)

    @field_validator("preconditions")
    @classmethod
    def validate_preconditions(
        cls, values: list[CommitmentPrecondition]
    ) -> list[CommitmentPrecondition]:
        ids = [value.belief_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("precondition belief references must be unique")
        return values

class CommitmentCreateRequest(_CommitmentDefinition):
    """Typed input for ``MemoryStore.add_commitment``.

    ``scope`` is optional at the API boundary only so missing context can
    return a structured fail-closed result. Definition validation still runs
    whenever a scope is supplied.
    """

    scope: Optional[str]


class Commitment(_CommitmentDefinition):
    id: int = Field(gt=0)
    scope: str
    created_by_agent_id: str
    state: CommitmentState
    proof_reference: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_by_agent_id")
    @classmethod
    def validate_creator(cls, value: str) -> str:
        return _commitment_text(value, "created_by_agent_id", 256)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_lifecycle_timestamp(cls, value: datetime, info) -> datetime:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_timestamp_order(self):
        if self.deadline < self.created_at:
            raise ValueError("deadline must not be before created_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be before created_at")
        return self


class CommitmentTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: int = Field(gt=0)
    target_state: str
    scope: Optional[str]
    task_type: Optional[str] = None
    proof_reference: Optional[str] = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value

class CommitmentListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Optional[str]
    task_type: Optional[str] = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value


class OverdueScanRequest(CommitmentListRequest):
    pass


class CommitmentExclusionSummary(BaseModel):
    reason_code: str
    rule_id: str
    count: int = Field(ge=0)
    safe_detail: str


class CommitmentMutationResult(BaseModel):
    ok: bool
    code: CommitmentResultCode
    message: str
    commitment: Optional[Commitment] = None
    trace_id: Optional[str] = None


class CommitmentListResult(BaseModel):
    authorized: bool
    code: CommitmentResultCode
    commitments: list[Commitment]
    exclusions: list[CommitmentExclusionSummary]


class OverdueScanResult(BaseModel):
    authorized: bool
    code: CommitmentResultCode
    as_of: Optional[datetime] = None
    overdue: list[Commitment]
    promoted_count: int = Field(ge=0)
    exclusions: list[CommitmentExclusionSummary]
    trace_id: Optional[str] = None


class ArtifactRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    execution_state: ArtifactExecutionState
    scope: Optional[str]
    label: str
    reference: Optional[str] = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        return _commitment_text(value, "label", 512)

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return _commitment_text(value, "reference", 2048)

    @model_validator(mode="after")
    def validate_kind_and_execution(self):
        if (
            self.kind == ArtifactKind.output
            and self.execution_state != ArtifactExecutionState.not_applicable
        ):
            raise ValueError("output artifacts require not_applicable execution state")
        if (
            self.kind == ArtifactKind.action
            and self.execution_state == ArtifactExecutionState.not_applicable
        ):
            raise ValueError("action artifacts require pending or executed state")
        return self


class Artifact(ArtifactRegistrationRequest):
    id: int = Field(gt=0)
    scope: str
    propagation_state: ArtifactPropagationState
    created_by_agent_id: str
    created_at: datetime
    updated_at: datetime

    @field_validator("created_by_agent_id")
    @classmethod
    def validate_creator(cls, value: str) -> str:
        return _commitment_text(value, "created_by_agent_id", 256)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_lifecycle_timestamp(cls, value: datetime, info) -> datetime:
        return _aware_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_timestamp_order(self):
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be before created_at")
        return self


class DependencyRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstream_kind: DependencyEndpointKind
    upstream_id: int = Field(gt=0)
    downstream_artifact_id: int = Field(gt=0)
    scope: Optional[str]
    task_type: Optional[str] = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value


class Dependency(BaseModel):
    id: int = Field(gt=0)
    upstream_kind: DependencyEndpointKind
    upstream_id: int = Field(gt=0)
    downstream_artifact_id: int = Field(gt=0)
    created_by_agent_id: str
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, "created_at")

    @field_validator("created_by_agent_id")
    @classmethod
    def validate_creator(cls, value: str) -> str:
        return _commitment_text(value, "created_by_agent_id", 256)


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    belief_id: int = Field(gt=0)
    kind: CorrectionKind
    content: str = Field(min_length=1, max_length=4096)
    scope: Optional[str]
    task_type: Optional[str] = None
    value: Optional[str] = None
    proposed_status: Optional[EpistemicStatus] = None

    @field_validator("content")
    @classmethod
    def validate_text(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Optional[str]) -> Optional[str]:
        return _nonempty(value, "value") if value is not None else None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value


class ArtifactRegistrationResult(BaseModel):
    ok: bool
    code: M6ResultCode
    message: str
    artifact: Optional[Artifact] = None
    trace_id: Optional[str] = None


class DependencyRegistrationResult(BaseModel):
    ok: bool
    code: M6ResultCode
    message: str
    dependency: Optional[Dependency] = None
    trace_id: Optional[str] = None


class ArtifactImpact(BaseModel):
    artifact: Artifact
    depth: int = Field(ge=1)
    previous_state: ArtifactPropagationState
    reason_code: M6ResultCode
    rule_id: str
    state_changed: bool


class HiddenImpactSummary(BaseModel):
    reason_code: M6ResultCode
    rule_id: str
    count: int = Field(ge=0)
    safe_detail: str


class CorrectionResult(BaseModel):
    ok: bool
    code: M6ResultCode
    message: str
    as_of: Optional[datetime] = None
    event: Optional[Event] = None
    belief: Optional[Belief] = None
    visible_impacts: list[ArtifactImpact] = Field(default_factory=list)
    hidden_impacts: list[HiddenImpactSummary] = Field(default_factory=list)
    affected_count: int = Field(default=0, ge=0)
    trace_id: Optional[str] = None

    @field_validator("as_of")
    @classmethod
    def validate_timestamp(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _aware_utc(value, "as_of") if value is not None else None


class Proposal(BaseModel):
    sequence: Optional[int] = Field(default=None, gt=0)
    id: str
    source_event_id: int = Field(gt=0)
    source_id: str
    source_type: str
    entity: str
    attribute: str
    value: str
    proposed_status: EpistemicStatus
    effective_status: EpistemicStatus
    scope: str
    decision_type: Optional[str] = None
    creator_agent_id: str
    created_at: datetime
    policy_version: int
    policy_fingerprint: str
    creation_trace_id: str
    expected_current_belief_id: Optional[int] = Field(default=None, gt=0)
    expected_current_absent: bool
    state: ProposalState = ProposalState.pending
    decision_actor_id: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_trace_id: Optional[str] = None
    approved_belief_id: Optional[int] = Field(default=None, gt=0)
    terminal_reason_code: Optional[str] = None

    @field_validator(
        "id",
        "source_id",
        "source_type",
        "entity",
        "attribute",
        "value",
        "creator_agent_id",
        "policy_fingerprint",
        "creation_trace_id",
    )
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("scope")
    @classmethod
    def validate_proposal_scope(cls, value: str) -> str:
        return _scope(value)

    @field_validator("created_at", "decided_at")
    @classmethod
    def validate_proposal_timestamp(
        cls, value: Optional[datetime], info
    ) -> Optional[datetime]:
        return _aware_utc(value, info.field_name) if value is not None else None

    @model_validator(mode="after")
    def validate_expected_current(self):
        if self.expected_current_absent == (self.expected_current_belief_id is not None):
            raise ValueError(
                "proposal must record exactly one of expected-current absence or ID"
            )
        if self.state == ProposalState.pending:
            if any(
                value is not None
                for value in (
                    self.decision_actor_id,
                    self.decided_at,
                    self.decision_trace_id,
                    self.approved_belief_id,
                    self.terminal_reason_code,
                )
            ):
                raise ValueError("pending proposal cannot contain terminal decision data")
        else:
            if not all(
                value is not None
                for value in (
                    self.decision_actor_id,
                    self.decided_at,
                    self.decision_trace_id,
                    self.terminal_reason_code,
                )
            ):
                raise ValueError("terminal proposal requires immutable decision provenance")
            if self.state == ProposalState.approved and self.approved_belief_id is None:
                raise ValueError("approved proposal requires approved belief ID")
            if self.state != ProposalState.approved and self.approved_belief_id is not None:
                raise ValueError("non-approved proposal cannot reference an approved belief")
        return self


class ProposalCreateResult(BaseModel):
    ok: bool
    code: ProposalResultCode
    message: str
    event: Optional[Event] = None
    proposals: list[Proposal] = Field(default_factory=list)
    trace_id: Optional[str] = None


class ProposalListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Optional[str]
    task_type: Optional[str] = None
    state: Optional[ProposalState] = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value


class ProposalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    scope: Optional[str]
    task_type: Optional[str] = None

    @field_validator("proposal_id")
    @classmethod
    def validate_proposal_id(cls, value: str) -> str:
        return _nonempty(value, "proposal_id")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value


class ProposalExclusionSummary(BaseModel):
    reason_code: str
    rule_id: str
    count: int = Field(ge=0)
    safe_detail: str


class ProposalListResult(BaseModel):
    authorized: bool
    code: ProposalResultCode
    proposals: list[Proposal] = Field(default_factory=list)
    exclusions: list[ProposalExclusionSummary] = Field(default_factory=list)


class ProposalDecisionResult(BaseModel):
    ok: bool
    code: ProposalResultCode
    message: str
    proposal: Optional[Proposal] = None
    belief: Optional[Belief] = None
    trace_id: Optional[str] = None


class IngestResult(BaseModel):
    ok: bool = True
    code: IngestResultCode = IngestResultCode.beliefs_committed
    event: Optional[Event] = None
    beliefs: list[Belief] = Field(default_factory=list)
    trace_id: Optional[str] = None


# --------------------------- policy models --------------------------------
# Loaded from trust_policy.yaml by policy.py (M3). Defined here so both
# policy.py and core.py can share one typed shape.


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrustRule(_PolicyModel):
    rule_id: str
    authoritative: list[str]
    ranking: list[str]

    @field_validator("rule_id")
    @classmethod
    def validate_rule_id(cls, value: str) -> str:
        return _nonempty(value, "rule_id")

    @model_validator(mode="after")
    def validate_sources(self) -> "TrustRule":
        if not self.ranking:
            raise ValueError("ranking must not be empty")
        if len(self.ranking) != len(set(self.ranking)):
            raise ValueError("ranking source types must be unique")
        if len(self.authoritative) != len(set(self.authoritative)):
            raise ValueError("authoritative source types must be unique")
        missing = set(self.authoritative) - set(self.ranking)
        if missing:
            raise ValueError(f"authoritative source types missing from ranking: {sorted(missing)}")
        return self


class GateRule(_PolicyModel):
    rule_id: str
    min_status: EpistemicStatus
    require_authoritative_source: bool
    require_uncontradicted: bool
    require_current: bool

    @field_validator("rule_id")
    @classmethod
    def validate_rule_id(cls, value: str) -> str:
        return _nonempty(value, "rule_id")


class ActionSpec(_PolicyModel):
    risk: RiskTier
    decision: str
    require_value: Optional[str] = None

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        return _nonempty(value, "decision")


class AgentPermissions(_PolicyModel):
    max_action_tier: RiskTier
    allowed_scopes: list[str]
    commitment_operations: list[CommitmentOperation] = Field(default_factory=list)
    memory_operations: list[MemoryOperation] = Field(default_factory=list)
    writable_source_ids: list[str] = Field(default_factory=list)

    @field_validator("allowed_scopes")
    @classmethod
    def validate_allowed_scopes(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("allowed_scopes must not be empty")
        if len(values) != len(set(values)):
            raise ValueError("allowed_scopes entries must be unique")
        for value in values:
            if value in {"project:*", "task_type:*"}:
                continue
            _scope(value)
        return values

    @field_validator("commitment_operations")
    @classmethod
    def validate_commitment_operations(
        cls, values: list[CommitmentOperation]
    ) -> list[CommitmentOperation]:
        if len(values) != len(set(values)):
            raise ValueError("commitment_operations entries must be unique")
        return values

    @field_validator("memory_operations")
    @classmethod
    def validate_memory_operations(
        cls, values: list[MemoryOperation]
    ) -> list[MemoryOperation]:
        if len(values) != len(set(values)):
            raise ValueError("memory_operations entries must be unique")
        return values

    @field_validator("writable_source_ids")
    @classmethod
    def validate_writable_source_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("writable_source_ids entries must be unique")
        for value in values:
            _nonempty(value, "writable_source_ids entry")
            if "*" in value:
                raise ValueError("writable_source_ids entries must be exact source IDs")
        return values


class TrustPolicy(_PolicyModel):
    version: int
    status_strength: dict[str, int]
    source_status_ceiling: dict[str, EpistemicStatus]
    source_principals: dict[str, str] = Field(default_factory=dict)
    trust_matrix: dict[str, TrustRule]
    risk_tiers: list[RiskTier]
    gate_rules: dict[RiskTier, GateRule]
    actions: dict[str, ActionSpec]
    agents: dict[str, AgentPermissions] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_internal_consistency(self) -> "TrustPolicy":
        expected_statuses = {status.value for status in EpistemicStatus}
        actual_statuses = set(self.status_strength)
        if actual_statuses != expected_statuses:
            raise ValueError(
                "status_strength must contain exactly the EpistemicStatus values; "
                f"missing={sorted(expected_statuses - actual_statuses)}, "
                f"unknown={sorted(actual_statuses - expected_statuses)}"
            )
        if any(strength < 0 for strength in self.status_strength.values()):
            raise ValueError("status_strength values must be non-negative")
        dead_statuses = {
            EpistemicStatus.superseded.value,
            EpistemicStatus.retracted.value,
            EpistemicStatus.do_not_use.value,
        }
        if any(self.status_strength[status] != 0 for status in dead_statuses):
            raise ValueError("superseded, retracted, and do_not_use must have strength 0")
        if any(
            self.status_strength[status] == 0
            for status in expected_statuses - dead_statuses
        ):
            raise ValueError("usable epistemic statuses must have positive strength")
        if self.risk_tiers != list(RiskTier):
            raise ValueError("risk_tiers must list all four tiers in increasing order")
        if set(self.gate_rules) != set(RiskTier):
            raise ValueError("gate_rules must define exactly one rule for every risk tier")

        configured_sources = set(self.source_status_ceiling)
        for source_id, source_type in self.source_principals.items():
            _nonempty(source_id, "source principal ID")
            if "*" in source_id:
                raise ValueError("source principal IDs must be exact")
            if source_type not in configured_sources:
                raise ValueError(
                    f"source principal {source_id!r} references unknown source type "
                    f"{source_type!r}"
                )
        known_source_ids = set(self.source_principals)
        for agent_id, permissions in self.agents.items():
            unknown_source_ids = set(permissions.writable_source_ids) - known_source_ids
            if unknown_source_ids:
                raise ValueError(
                    f"agent {agent_id!r} references unknown writable source IDs: "
                    f"{sorted(unknown_source_ids)}"
                )
        for decision_type, rule in self.trust_matrix.items():
            _nonempty(decision_type, "decision_type")
            unknown_sources = set(rule.ranking) - configured_sources
            if unknown_sources:
                raise ValueError(
                    f"trust rule {rule.rule_id!r} references unknown source types: "
                    f"{sorted(unknown_sources)}"
                )

        for action, spec in self.actions.items():
            _nonempty(action, "action")
            if spec.decision not in self.trust_matrix:
                raise ValueError(
                    f"action {action!r} references unknown decision type {spec.decision!r}"
                )

        rule_ids = [rule.rule_id for rule in self.trust_matrix.values()]
        rule_ids.extend(rule.rule_id for rule in self.gate_rules.values())
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("policy rule_id values must be unique")
        return self


# ------------------------------- outputs -----------------------------------


class ConflictResolution(BaseModel):
    winner: Belief
    losers: list[Belief]
    rule_id: str
    contradicted: bool
    reason_code: str


class PolicyReason(BaseModel):
    code: str
    message: str
    rule_id: Optional[str] = None


class GateResult(BaseModel):
    ok: bool = True
    result_code: AuditResultCode = AuditResultCode.gate_evaluated
    decision: GateDecision
    action: str
    decision_type: Optional[str]
    risk_tier: Optional[RiskTier]
    rule_ids: list[str]
    reason_codes: list[str]
    reasons: list[str]
    details: list[PolicyReason]
    trace_id: Optional[str] = None
    trace_persisted: Optional[bool] = None


class RetrievalRequest(BaseModel):
    """Typed public input for ``MemoryStore.retrieve``."""

    model_config = ConfigDict(extra="forbid")

    query: str = ""
    entity: Optional[str] = None
    attribute: Optional[str] = None
    scope: str
    task_type: Optional[str] = None
    status_floor: Optional[EpistemicStatus] = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return _scope(value)

    @field_validator("entity", "attribute")
    @classmethod
    def validate_optional_filter(cls, value: Optional[str], info) -> Optional[str]:
        return _nonempty(value, info.field_name) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value


class AssemblyRequest(RetrievalRequest):
    """Typed public input for ``MemoryStore.assemble``."""

    token_budget: int = Field(default=1024, ge=1)


class RankFactors(BaseModel):
    query_entity_hits: int
    query_attribute_hits: int
    query_value_hits: int
    scope_relevance: int
    task_relevance: int
    status_strength: int
    recency_micros: int
    tie_break_id: int


class RetrievedBelief(BaseModel):
    belief: Belief
    source: Source
    rank: int
    rank_factors: RankFactors
    admitted_by: list[str]


class DedupDecision(BaseModel):
    rule_id: str
    representative_id: int
    dropped_ids: list[int]


class ExclusionSummary(BaseModel):
    reason_code: str
    rule_id: str
    count: int = Field(ge=0)
    safe_detail: str


class RetrievalResult(BaseModel):
    request: RetrievalRequest
    agent_id: str
    authorized: bool
    effective_scopes: list[str]
    query_tokens: list[str]
    items: list[RetrievedBelief]
    exclusions: list[ExclusionSummary]
    deduplications: list[DedupDecision]


class ConflictGroup(BaseModel):
    group_id: str
    entity: str
    attribute: str
    belief_ids: list[int]
    winner_id: Optional[int]
    rule_id: str
    reason_code: str


class PermissionEntry(BaseModel):
    entity: str
    attribute: str
    action: str
    decision: GateDecision
    risk_tier: RiskTier
    decision_type: str
    gate: GateResult


class ReceiptBelief(BaseModel):
    belief_id: int
    status: EpistemicStatus
    source_id: str
    source_type: str
    scope: str
    admitted_by: list[str]
    rank: int
    rank_factors: RankFactors
    conflict_group_id: Optional[str] = None


class TokenMeter(BaseModel):
    used: int
    budget: int
    method: str
    rule_id: str


class MemoryReceipt(BaseModel):
    included: list[ReceiptBelief]
    exclusions: list[ExclusionSummary]
    deduplications: list[DedupDecision]
    conflict_rule_ids: list[str]
    permission_rule_ids: list[str]
    rule_ids: list[str]
    tokens: TokenMeter


class AssembledContext(BaseModel):
    request: AssemblyRequest
    ok: bool = True
    result_code: AuditResultCode = AuditResultCode.context_assembled
    text: str = ""
    rendered_receipt: str = ""
    items: list[RetrievedBelief] = Field(default_factory=list)
    conflicts: list[ConflictGroup] = Field(default_factory=list)
    permissions: list[PermissionEntry] = Field(default_factory=list)
    receipt: Optional[MemoryReceipt] = None
    tokens_injected: int = 0
    token_budget: int
    trace_id: Optional[str] = None
    trace_persisted: Optional[bool] = None


# -------------------------- M7 audit / explain -----------------------------


class PolicySnapshot(BaseModel):
    version: int
    fingerprint: str

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        value = _nonempty(value, "fingerprint")
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("policy fingerprint must be a lowercase SHA-256 digest")
        return value


class BeliefEvidenceSnapshot(BaseModel):
    belief_id: int = Field(gt=0)
    entity: str
    attribute: str
    value: str
    status: EpistemicStatus
    scope: str
    source_id: str
    source_type: str
    source_label: str
    event_id: Optional[int] = Field(default=None, gt=0)
    supersedes_id: Optional[int] = Field(default=None, gt=0)
    decision_type: Optional[str] = None
    valid_from: str
    created_at: str
    was_structurally_current: bool

    @field_validator(
        "entity",
        "attribute",
        "value",
        "source_id",
        "source_type",
        "source_label",
        "valid_from",
        "created_at",
    )
    @classmethod
    def validate_snapshot_text(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("scope")
    @classmethod
    def validate_snapshot_scope(cls, value: str) -> str:
        return _scope(value)


class EvidenceUseSnapshot(BaseModel):
    evidence: BeliefEvidenceSnapshot
    rank: int = Field(gt=0)
    rank_factors: RankFactors
    admitted_by: list[str]
    conflict_group_id: Optional[str] = None


class ConflictStateSnapshot(BaseModel):
    group_id: str
    belief_ids: list[int]
    winner_id: Optional[int] = None
    rule_id: Optional[str] = None
    conflicted: bool


class PermissionChange(BaseModel):
    entity: str
    attribute: str
    action: str
    before: Optional[GateDecision] = None
    after: Optional[GateDecision] = None
    before_rule_ids: list[str] = Field(default_factory=list)
    after_rule_ids: list[str] = Field(default_factory=list)
    before_reason_codes: list[str] = Field(default_factory=list)
    after_reason_codes: list[str] = Field(default_factory=list)


class ConflictChange(BaseModel):
    group_id: str
    before: Optional[ConflictStateSnapshot] = None
    after: Optional[ConflictStateSnapshot] = None


class CounterfactualResult(BaseModel):
    belief_id: Optional[int] = Field(default=None, gt=0)
    code: CounterfactualCode
    remaining_belief_ids: list[int] = Field(default_factory=list)
    gate_before: Optional[GateDecision] = None
    gate_after: Optional[GateDecision] = None
    reason_codes_before: list[str] = Field(default_factory=list)
    reason_codes_after: list[str] = Field(default_factory=list)
    permission_changes: list[PermissionChange] = Field(default_factory=list)
    conflict_changes: list[ConflictChange] = Field(default_factory=list)


class AssemblyAuditSnapshot(BaseModel):
    request: AssemblyRequest
    evidence: list[EvidenceUseSnapshot]
    conflicts: list[ConflictGroup]
    permissions: list[PermissionEntry]
    receipt: MemoryReceipt
    rendered_receipt: str
    tokens_injected: int = Field(ge=0)
    token_budget: int = Field(gt=0)


class GateAuditSnapshot(BaseModel):
    action: str
    entity: str
    request_scope: str
    task_type: Optional[str] = None
    evidence: list[BeliefEvidenceSnapshot]
    result: GateResult
    exclusions: list[ExclusionSummary] = Field(default_factory=list)
    conflict: Optional[ConflictStateSnapshot] = None

    @field_validator("request_scope")
    @classmethod
    def validate_request_scope(cls, value: str) -> str:
        return _scope(value)


class ProposalAuditSnapshot(BaseModel):
    proposal_id: str
    source_event_id: int = Field(gt=0)
    source_id: str
    source_type: str
    entity: str
    attribute: str
    value: str
    proposed_status: EpistemicStatus
    effective_status: EpistemicStatus
    scope: str
    decision_type: Optional[str] = None
    creator_agent_id: str
    policy_version: int
    policy_fingerprint: str
    expected_current_belief_id: Optional[int] = Field(default=None, gt=0)
    expected_current_absent: bool
    state: ProposalState

    @field_validator(
        "proposal_id", "source_id", "source_type", "entity", "attribute",
        "value", "creator_agent_id", "policy_fingerprint",
    )
    @classmethod
    def validate_proposal_audit_text(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("scope")
    @classmethod
    def validate_proposal_audit_scope(cls, value: str) -> str:
        return _scope(value)


class ArtifactImpactAuditSnapshot(BaseModel):
    artifact_id: int = Field(gt=0)
    depth: int = Field(ge=1)
    previous_state: ArtifactPropagationState
    new_state: ArtifactPropagationState
    reason_code: M6ResultCode
    rule_id: str
    state_changed: bool


class MutationAuditSnapshot(BaseModel):
    event_ids: list[int] = Field(default_factory=list)
    belief_ids: list[int] = Field(default_factory=list)
    beliefs: list[BeliefEvidenceSnapshot] = Field(default_factory=list)
    proposal_ids: list[str] = Field(default_factory=list)
    proposals: list[ProposalAuditSnapshot] = Field(default_factory=list)
    target_belief: Optional[BeliefEvidenceSnapshot] = None
    commitment_ids: list[int] = Field(default_factory=list)
    artifact_ids: list[int] = Field(default_factory=list)
    dependency_ids: list[int] = Field(default_factory=list)
    previous_state: Optional[str] = None
    new_state: Optional[str] = None
    promoted_commitment_ids: list[int] = Field(default_factory=list)
    observed_current_belief_ids: list[int] = Field(default_factory=list)
    visible_impact_artifact_ids: list[int] = Field(default_factory=list)
    visible_impacts: list[ArtifactImpactAuditSnapshot] = Field(default_factory=list)
    hidden_impact_count: int = Field(default=0, ge=0)
    affected_count: int = Field(default=0, ge=0)


class AuditPayload(BaseModel):
    assembly: Optional[AssemblyAuditSnapshot] = None
    gate: Optional[GateAuditSnapshot] = None
    mutation: Optional[MutationAuditSnapshot] = None
    counterfactuals: list[CounterfactualResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_one_snapshot(self):
        populated = sum(
            value is not None for value in (self.assembly, self.gate, self.mutation)
        )
        if populated != 1:
            raise ValueError("audit payload requires exactly one typed operation snapshot")
        return self


class AuditTrace(BaseModel):
    sequence: Optional[int] = Field(default=None, gt=0)
    trace_id: str
    session_id: str
    session_mode: SessionMode
    agent_id: str
    approval_actor_id: Optional[str] = None
    active_scope: str
    task_type: Optional[str] = None
    operation: AuditOperation
    outcome: AuditOutcome
    result_code: str
    reason_codes: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    policy: PolicySnapshot
    payload: AuditPayload
    persisted: bool
    created_at: datetime

    @field_validator(
        "trace_id", "session_id", "agent_id", "result_code"
    )
    @classmethod
    def validate_trace_text(cls, value: str, info) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("approval_actor_id")
    @classmethod
    def validate_optional_actor(cls, value: Optional[str]) -> Optional[str]:
        return _nonempty(value, "approval_actor_id") if value is not None else None

    @field_validator("active_scope")
    @classmethod
    def validate_active_scope(cls, value: str) -> str:
        return _scope(value)

    @field_validator("task_type")
    @classmethod
    def validate_trace_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_trace_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, "created_at")

    @field_validator("reason_codes", "rule_ids")
    @classmethod
    def validate_unique_codes(cls, values: list[str], info) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError(f"{info.field_name} entries must be unique")
        for value in values:
            _nonempty(value, f"{info.field_name} entry")
        return values

    @model_validator(mode="after")
    def validate_persistence_mode(self):
        if self.persisted == (self.session_mode == SessionMode.ephemeral):
            raise ValueError(
                "ephemeral traces must be transient and durable traces non-ephemeral"
            )
        return self

    @model_validator(mode="after")
    def validate_operation_snapshot_consistency(self):
        if self.operation == AuditOperation.assemble:
            if self.payload.assembly is None:
                raise ValueError("assembly trace requires an assembly snapshot")
            if self.active_scope != self.payload.assembly.request.scope:
                raise ValueError("trace scope does not match assembly request scope")
            if self.task_type != self.payload.assembly.request.task_type:
                raise ValueError("trace task type does not match assembly request")
            assembly = self.payload.assembly
            evidence = {
                item.evidence.belief_id: item.evidence for item in assembly.evidence
            }
            receipt = {item.belief_id: item for item in assembly.receipt.included}
            if set(evidence) != set(receipt):
                raise ValueError("assembly receipt does not match historical evidence")
            for belief_id, item in receipt.items():
                snapshot = evidence[belief_id]
                if (
                    item.status != snapshot.status
                    or item.source_id != snapshot.source_id
                    or item.source_type != snapshot.source_type
                    or item.scope != snapshot.scope
                ):
                    raise ValueError("assembly receipt evidence metadata is inconsistent")
            evidence_ids = set(evidence)
            if any(
                not set(conflict.belief_ids).issubset(evidence_ids)
                for conflict in assembly.conflicts
            ):
                raise ValueError("assembly conflict references evidence not in the trace")
            evidence_keys = {
                (item.entity, item.attribute) for item in evidence.values()
            }
            if any(
                (permission.entity, permission.attribute) not in evidence_keys
                for permission in assembly.permissions
            ):
                raise ValueError("assembly permission references evidence not in the trace")
            if (
                assembly.receipt.tokens.used != assembly.tokens_injected
                or assembly.receipt.tokens.budget != assembly.token_budget
                or assembly.request.token_budget != assembly.token_budget
            ):
                raise ValueError("assembly token snapshot is inconsistent")
        elif self.operation == AuditOperation.gate:
            if self.payload.gate is None:
                raise ValueError("gate trace requires a gate snapshot")
            if self.active_scope != self.payload.gate.request_scope:
                raise ValueError("trace scope does not match gate request scope")
            if self.task_type != self.payload.gate.task_type:
                raise ValueError("trace task type does not match gate request")
        else:
            if self.payload.mutation is None:
                raise ValueError("mutation trace requires a mutation snapshot")
            if self.payload.counterfactuals:
                raise ValueError("mutation traces cannot contain belief counterfactuals")
            mutation = self.payload.mutation
            if self.operation == AuditOperation.ingest and set(mutation.belief_ids) != {
                item.belief_id for item in mutation.beliefs
            }:
                raise ValueError("ingest trace belief references are inconsistent")
            if self.operation in {
                AuditOperation.proposal_create,
                AuditOperation.proposal_approve,
                AuditOperation.proposal_reject,
            } and set(mutation.proposal_ids) != {
                item.proposal_id for item in mutation.proposals
            }:
                raise ValueError("proposal trace references are inconsistent")
            if self.operation == AuditOperation.correction and mutation.target_belief is None:
                raise ValueError("correction trace requires its historical target snapshot")
        return self


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    scope: Optional[str]
    task_type: Optional[str] = None
    belief_id: Optional[int] = Field(default=None, gt=0)

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        return _nonempty(value, "trace_id")

    @field_validator("scope")
    @classmethod
    def validate_explain_scope(cls, value: Optional[str]) -> Optional[str]:
        return _scope(value) if value is not None else None

    @field_validator("task_type")
    @classmethod
    def validate_explain_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = _nonempty(value, "task_type")
        if ":" in value:
            raise ValueError("task_type must be the bare task name, not a scope string")
        return value


class CurrentBeliefFollowUp(BaseModel):
    belief_id: int = Field(gt=0)
    code: StructuralFollowUpCode
    current_belief_id: Optional[int] = Field(default=None, gt=0)
    current_status: Optional[EpistemicStatus] = None
    rule_id: str = "M7-STRUCTURAL-FOLLOW-UP-001"


class ExplainResult(BaseModel):
    authorized: bool
    code: ExplainResultCode
    trace_id: str
    trace: Optional[AuditTrace] = None
    counterfactual: Optional[CounterfactualResult] = None
    current_follow_up: list[CurrentBeliefFollowUp] = Field(default_factory=list)
    rendered: str = ""
