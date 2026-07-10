"""Pydantic v2 models for Epistemic Memory. See PLAN.md §2 for rationale."""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel


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


class Source(BaseModel):
    id: str
    type: str
    label: str
    created_at: str


class Event(BaseModel):
    id: Optional[int] = None
    source_id: str
    content: str
    scope: str
    meta: Optional[dict] = None
    created_at: str


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


class TrustRule(BaseModel):
    rule_id: str
    authoritative: list[str]
    ranking: list[str]


class GateRule(BaseModel):
    min_status: EpistemicStatus
    require_authoritative_source: bool
    require_uncontradicted: bool
    require_current: bool


class ActionSpec(BaseModel):
    risk: RiskTier
    decision: str
    require_value: Optional[str] = None


class AgentPermissions(BaseModel):
    max_tier: RiskTier
    allowed_scopes: list[str]


class TrustPolicy(BaseModel):
    version: int
    status_strength: dict[str, int]
    source_status_ceiling: dict[str, EpistemicStatus]
    trust_matrix: dict[str, TrustRule]
    gate_rules: dict[RiskTier, GateRule]
    actions: dict[str, ActionSpec]
    agents: dict[str, AgentPermissions] = {}


# ------------------------------- outputs -----------------------------------


class ConflictResolution(BaseModel):
    winner: Belief
    losers: list[Belief]
    rule_id: str
    contradicted: bool


class GateResult(BaseModel):
    decision: GateDecision
    reasons: list[str]


class ReceiptLine(BaseModel):
    belief_id: int
    status: EpistemicStatus
    source_id: str
    scope: str
    admitted_by: str


class AssembledContext(BaseModel):
    text: str
    receipt: list[ReceiptLine]
    tokens_injected: int
    conflicts: list[str]
