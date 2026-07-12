"""Reference adapter using only the public MemoryStore service boundary."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from importlib import resources
from tempfile import TemporaryDirectory

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    AssemblyRequest,
    CandidateBelief,
    ExplainRequest,
    RetrievalRequest,
    Source,
)
from epistemic_memory.policy import load_policy

from ..models import BenchmarkCase, Observation, ObservedBelief


_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


class _Ids:
    def __init__(self, prefix: str):
        self._prefix = prefix
        self._next = 0

    def __call__(self, kind: str) -> str:
        self._next += 1
        return f"bench-{self._prefix}-{kind}-{self._next:03d}"


def _observed(belief) -> ObservedBelief:
    return ObservedBelief(
        value=belief.value,
        status=belief.status,
        scope=belief.scope,
    )


def _extractor(step):
    def extract(_event, _source_type):
        candidate = step.candidate
        return [CandidateBelief(
            entity=candidate.entity,
            attribute=candidate.attribute,
            value=candidate.value,
            proposed_status=candidate.proposed_status,
            scope=candidate.scope,
            decision_type=candidate.decision_type,
        )]

    return extract


class OursAdapter:
    name = "ours"

    def evaluate(self, case: BenchmarkCase) -> Observation:
        policy_resource = resources.files("memgov_bench.data").joinpath("policy.yaml")
        with resources.as_file(policy_resource) as policy_path, TemporaryDirectory() as temp:
            policy = load_policy(str(policy_path))
            database = f"{temp}/case.db"
            sources = [
                Source(
                    id="user",
                    type="user",
                    label="Synthetic user channel",
                    created_at=_NOW.isoformat(),
                ),
                Source(
                    id="billing",
                    type="billing_system",
                    label="Synthetic billing fixture",
                    created_at=_NOW.isoformat(),
                ),
                Source(
                    id="untrusted",
                    type="untrusted_channel",
                    label="Synthetic untrusted channel",
                    created_at=_NOW.isoformat(),
                ),
            ]
            with ExitStack() as stack:
                sessions = {}
                for index, agent_id in enumerate(
                    ("support-agent", "billing-ingestor", "untrusted-ingestor")
                ):
                    memory = MemoryStore(
                        database,
                        policy,
                        agent_id=agent_id,
                        session_id=f"bench-{case.case_id}-{agent_id}",
                        clock=lambda: _NOW,
                        id_factory=_Ids(f"{case.case_id}-{index}"),
                        trusted_sources=sources if index == 0 else None,
                    )
                    stack.callback(memory.close)
                    sessions[agent_id] = memory

                ingested = []
                for step in case.setup:
                    result = sessions[step.actor].ingest(
                        source_id=step.source_id,
                        content=step.content,
                        scope=step.scope,
                        extractor=_extractor(step),
                    )
                    if not result.ok or len(result.beliefs) != 1:
                        raise RuntimeError("controlled benchmark ingest did not commit one belief")
                    ingested.append(_observed(result.beliefs[0]))

                probe = case.probe
                reference = sessions["support-agent"]
                retrieval = reference.retrieve(RetrievalRequest(
                    entity=probe.entity,
                    attribute=probe.attribute,
                    scope=probe.scope,
                    task_type=probe.task_type,
                ))
                retrieved = tuple(_observed(item.belief) for item in retrieval.items)

                assembled = ()
                if probe.assemble:
                    context = reference.assemble(AssemblyRequest(
                        entity=probe.entity,
                        attribute=probe.attribute,
                        scope=probe.scope,
                        task_type=probe.task_type,
                        token_budget=2000,
                    ))
                    if not context.ok:
                        raise RuntimeError("controlled benchmark assembly failed")
                    assembled = tuple(_observed(item.belief) for item in context.items)

                decision = None
                gate_rule_ids = ()
                trace_authorized = False
                trace_evidence_count = 0
                trace_rule_ids = ()
                if probe.gate_action is not None:
                    gate = reference.gate(
                        action=probe.gate_action,
                        entity=probe.entity,
                        scope=probe.scope,
                        task_type=probe.task_type,
                    )
                    decision = gate.decision
                    gate_rule_ids = tuple(gate.rule_ids)
                    if probe.explain_gate:
                        if gate.trace_id is None:
                            raise RuntimeError("controlled benchmark gate omitted its trace")
                        explanation = reference.explain(ExplainRequest(
                            trace_id=gate.trace_id,
                            scope=probe.scope,
                            task_type=probe.task_type,
                        ))
                        trace_authorized = explanation.authorized
                        if explanation.trace is not None:
                            trace_rule_ids = tuple(explanation.trace.rule_ids)
                            snapshot = explanation.trace.payload.gate
                            trace_evidence_count = len(snapshot.evidence) if snapshot else 0

                return Observation(
                    ingested=tuple(ingested),
                    retrieved=retrieved,
                    assembled=assembled,
                    decision=decision,
                    gate_rule_ids=gate_rule_ids,
                    trace_authorized=trace_authorized,
                    trace_evidence_count=trace_evidence_count,
                    trace_rule_ids=trace_rule_ids,
                )
