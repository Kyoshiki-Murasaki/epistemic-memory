"""Pydantic v2 models for Epistemic Memory. See PLAN.md §2 for rationale."""

from __future__ import annotations

import re
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


_SCOPE_RE = re.compile(r"^(global|persona)$|^(project|task_type):(.+)$")


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


class Commitment(BaseModel):
    id: Optional[int] = None
    description: str
    owner: str
    beneficiary: str
    state: CommitmentState
    deadline: Optional[str] = None
    preconditions: Optional[str] = None
    proof_belief_id: Optional[int] = None
    created_at: str
    updated_at: str


class Artifact(BaseModel):
    id: Optional[int] = None
    kind: str
    ref: str
    state: str
    created_at: str


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
