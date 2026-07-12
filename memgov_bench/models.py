"""Typed benchmark contract shared by fixtures, adapters, and scoring."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from epistemic_memory.models import EpistemicStatus, GateDecision


RUN_COUNT = 3


class Dimension(str, Enum):
    stale_fact_leakage = "stale-fact leakage"
    claim_fact_confusion = "claim/fact confusion"
    scope_leakage = "scope leakage"
    injection_resistance = "injection resistance"
    gate_correctness = "gate correctness"


DIMENSIONS: tuple[Dimension, ...] = tuple(Dimension)


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Candidate(_ContractModel):
    entity: str = Field(min_length=1, max_length=128)
    attribute: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)
    proposed_status: EpistemicStatus
    scope: str = Field(min_length=1, max_length=128)
    decision_type: Optional[str] = Field(default=None, min_length=1, max_length=128)


class IngestStep(_ContractModel):
    actor: Literal["support-agent", "billing-ingestor", "untrusted-ingestor"]
    source_id: Literal["user", "billing", "untrusted"]
    content: str = Field(min_length=1, max_length=1024)
    scope: str = Field(min_length=1, max_length=128)
    candidate: Candidate

    @model_validator(mode="after")
    def validate_actor_source_pair(self) -> "IngestStep":
        permitted = {
            "support-agent": "user",
            "billing-ingestor": "billing",
            "untrusted-ingestor": "untrusted",
        }
        if permitted[self.actor] != self.source_id:
            raise ValueError("fixture actor is not authorized for source_id")
        if self.scope != self.candidate.scope:
            raise ValueError("event and candidate scopes must match")
        return self


class Probe(_ContractModel):
    entity: str = Field(min_length=1, max_length=128)
    attribute: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=128)
    task_type: Optional[str] = Field(default=None, min_length=1, max_length=128)
    assemble: bool
    gate_action: Optional[str] = Field(default=None, min_length=1, max_length=128)
    explain_gate: bool

    @model_validator(mode="after")
    def validate_explain_requires_gate(self) -> "Probe":
        if self.explain_gate and self.gate_action is None:
            raise ValueError("explain_gate requires gate_action")
        return self


class ObservedBelief(_ContractModel):
    value: str
    status: EpistemicStatus
    scope: str


class Observation(_ContractModel):
    ingested: tuple[ObservedBelief, ...]
    retrieved: tuple[ObservedBelief, ...]
    assembled: tuple[ObservedBelief, ...]
    decision: Optional[GateDecision]
    gate_rule_ids: tuple[str, ...]
    trace_authorized: bool
    trace_evidence_count: int = Field(ge=0)
    trace_rule_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_gate_and_trace_shape(self) -> "Observation":
        if self.decision is None and (
            self.gate_rule_ids
            or self.trace_authorized
            or self.trace_evidence_count
            or self.trace_rule_ids
        ):
            raise ValueError("non-gate observation cannot contain gate or trace data")
        if self.trace_authorized and self.decision is None:
            raise ValueError("authorized trace requires a gate decision")
        return self


class BenchmarkCase(_ContractModel):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    dimension: Dimension
    description: str = Field(min_length=1, max_length=512)
    setup: tuple[IngestStep, ...] = Field(min_length=1, max_length=8)
    probe: Probe
    expected: Observation

    @field_validator("description")
    @classmethod
    def validate_synthetic_description(cls, value: str) -> str:
        if "@" in value:
            raise ValueError("fixture descriptions must not contain personal identifiers")
        return value


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    dimension: Dimension
    passed: bool


@dataclass(frozen=True)
class DimensionResult:
    dimension: Dimension
    passed_cases: int
    failed_cases: int
    total_cases: int
    passed: bool

    @property
    def score_percent(self) -> float:
        if self.total_cases <= 0:
            raise ValueError("dimension cannot have zero cases")
        return self.passed_cases * 100.0 / self.total_cases


@dataclass(frozen=True)
class RunResult:
    run_number: int
    cases: tuple[CaseResult, ...]
    dimensions: tuple[DimensionResult, ...]
    overall_passed: bool

    @property
    def passed_cases(self) -> int:
        return sum(result.passed_cases for result in self.dimensions)

    @property
    def total_cases(self) -> int:
        return sum(result.total_cases for result in self.dimensions)

    @property
    def score_percent(self) -> float:
        if self.total_cases <= 0:
            raise ValueError("benchmark cannot have zero cases")
        return self.passed_cases * 100.0 / self.total_cases


@dataclass(frozen=True)
class ScoreStatistics:
    mean: float
    minimum: float
    maximum: float
    variance: float


@dataclass(frozen=True)
class BenchmarkReport:
    adapter_name: str
    runs: tuple[RunResult, ...]
    dimension_statistics: tuple[tuple[Dimension, ScoreStatistics], ...]
    overall_statistics: ScoreStatistics
    correctness_differs: bool
    overall_passed: bool
