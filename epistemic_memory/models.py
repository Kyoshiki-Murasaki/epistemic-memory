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
    invalid_correction = "invalid_correction"
    artifact_not_found = "artifact_not_found"
    dependency_endpoint_not_found = "dependency_endpoint_not_found"
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


class CommitmentListResult(BaseModel):
    authorized: bool
    code: CommitmentResultCode
    commitments: list[Commitment]
    exclusions: list[CommitmentExclusionSummary]


class OverdueScanResult(BaseModel):
    authorized: bool
    code: CommitmentResultCode
    as_of: datetime
    overdue: list[Commitment]
    promoted_count: int = Field(ge=0)
    exclusions: list[CommitmentExclusionSummary]


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
    source_id: str
    content: str = Field(min_length=1, max_length=4096)
    scope: Optional[str]
    task_type: Optional[str] = None
    value: Optional[str] = None
    proposed_status: Optional[EpistemicStatus] = None

    @field_validator("source_id", "content")
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


class DependencyRegistrationResult(BaseModel):
    ok: bool
    code: M6ResultCode
    message: str
    dependency: Optional[Dependency] = None


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
    as_of: datetime
    event: Optional[Event] = None
    belief: Optional[Belief] = None
    visible_impacts: list[ArtifactImpact] = Field(default_factory=list)
    hidden_impacts: list[HiddenImpactSummary] = Field(default_factory=list)
    affected_count: int = Field(default=0, ge=0)

    @field_validator("as_of")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _aware_utc(value, "as_of")


class IngestResult(BaseModel):
    event: Event
    beliefs: list[Belief]


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


class TrustPolicy(_PolicyModel):
    version: int
    status_strength: dict[str, int]
    source_status_ceiling: dict[str, EpistemicStatus]
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
    decision: GateDecision
    action: str
    decision_type: Optional[str]
    risk_tier: Optional[RiskTier]
    rule_ids: list[str]
    reason_codes: list[str]
    reasons: list[str]
    details: list[PolicyReason]


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
    text: str
    rendered_receipt: str
    items: list[RetrievedBelief]
    conflicts: list[ConflictGroup]
    permissions: list[PermissionEntry]
    receipt: MemoryReceipt
    tokens_injected: int
    token_budget: int
