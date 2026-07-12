"""Minimal deterministic negative control used to validate benchmark sensitivity."""

from __future__ import annotations

from epistemic_memory.models import GateDecision

from ..models import BenchmarkCase, Observation, ObservedBelief


class AlwaysDenyAdapter:
    """A weak participant that retains every input and denies every requested action."""

    name = "always-deny-control"

    def evaluate(self, case: BenchmarkCase) -> Observation:
        beliefs = tuple(
            ObservedBelief(
                value=step.candidate.value,
                status=step.candidate.proposed_status,
                scope=step.candidate.scope,
            )
            for step in case.setup
        )
        decision = GateDecision.deny if case.probe.gate_action is not None else None
        return Observation(
            ingested=beliefs,
            retrieved=beliefs,
            assembled=beliefs if case.probe.assemble else (),
            decision=decision,
            gate_rule_ids=("CONTROL-ALWAYS-DENY",) if decision else (),
            trace_authorized=False,
            trace_evidence_count=0,
            trace_rule_ids=(),
        )
