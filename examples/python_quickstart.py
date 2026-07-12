"""Executable direct, proposal, approval, ephemeral, and explain workflow."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    AssemblyRequest,
    CandidateBelief,
    CorrectionRequest,
    ExplainRequest,
    ProposalDecisionRequest,
    ProposalListRequest,
    RetrievalRequest,
    SessionMode,
    Source,
)
from epistemic_memory.policy import load_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "examples" / "trust_policy.yaml"
SOURCE = Source(
    id="user",
    type="user",
    label="User-provided statements",
    created_at="2026-07-12T00:00:00+00:00",
)


def candidate(value: str) -> CandidateBelief:
    return CandidateBelief(
        entity="customer_881",
        attribute="preferred_name",
        value=value,
        proposed_status="system_verified",
        scope="global",
        decision_type="preferred_name",
    )


def fixture(value: str):
    def extract(_event, _source_type):
        return [candidate(value)]

    return extract


def run() -> dict[str, object]:
    policy = load_policy(str(POLICY_PATH))
    with tempfile.TemporaryDirectory(prefix="epistemic-memory-example-") as directory:
        database = Path(directory) / "memory.db"

        direct = MemoryStore(
            str(database),
            policy,
            agent_id="support-agent",
            trusted_sources=[SOURCE],
        )
        ingested = direct.ingest(
            source_id="user",
            content="Please call me Sam",
            scope="global",
            extractor=fixture("Sam"),
        )
        assert ingested.ok and ingested.beliefs[0].status.value == "user_stated"

        retrieved = direct.retrieve(
            RetrievalRequest(entity="customer_881", scope="global")
        )
        assembled = direct.assemble(
            AssemblyRequest(entity="customer_881", scope="global", token_budget=1200)
        )
        gated = direct.gate(
            action="update_preferred_name",
            entity="customer_881",
            scope="global",
        )
        explained = direct.explain(
            ExplainRequest(trace_id=gated.trace_id, scope="global")
        )
        corrected = direct.correct(
            CorrectionRequest(
                belief_id=ingested.beliefs[0].id,
                kind="correction",
                content="The user corrected their preferred name",
                scope="global",
                value="Ari",
                proposed_status="system_verified",
            )
        )
        assert retrieved.authorized and retrieved.items
        assert assembled.ok and assembled.trace_id
        assert gated.decision.value == "allow"
        assert explained.authorized
        assert corrected.ok and corrected.belief.status.value == "user_stated"
        direct.close()

        proposer = MemoryStore(
            str(database),
            policy,
            agent_id="support-agent",
            session_mode=SessionMode.propose,
        )
        proposed = proposer.ingest(
            source_id="user",
            content="Actually, use Rae",
            scope="global",
            extractor=fixture("Rae"),
        )
        assert proposed.ok and proposed.proposals[0].state.value == "pending"
        proposer.close()

        approver = MemoryStore(
            str(database),
            policy,
            agent_id="support-agent",
            approval_actor_id="human-reviewer",
        )
        pending = approver.list_proposals(ProposalListRequest(scope="global"))
        approved = approver.approve_proposal(
            ProposalDecisionRequest(
                proposal_id=pending.proposals[0].id,
                scope="global",
            )
        )
        assert pending.authorized and approved.ok
        approver.close()

        ephemeral = MemoryStore(
            str(database),
            policy,
            agent_id="support-agent",
            session_mode=SessionMode.ephemeral,
        )
        ephemeral_read = ephemeral.retrieve(
            RetrievalRequest(entity="customer_881", scope="global")
        )
        transient = ephemeral.assemble(
            AssemblyRequest(entity="customer_881", scope="global", token_budget=1200)
        )
        transient_explanation = ephemeral.explain(
            ExplainRequest(trace_id=transient.trace_id, scope="global")
        )
        blocked = ephemeral.ingest(
            source_id="user",
            content="This must not persist",
            scope="global",
            extractor=fixture("Blocked"),
        )
        assert ephemeral_read.authorized and ephemeral_read.items
        assert transient.trace_persisted is False
        assert transient_explanation.authorized
        assert blocked.code.value == "ephemeral_write_blocked"
        ephemeral.close()

        return {
            "approved_value": approved.belief.value,
            "correction_code": corrected.code.value,
            "direct_gate": gated.decision.value,
            "ephemeral_write": blocked.code.value,
            "proposal_code": approved.code.value,
        }


def main() -> int:
    print(json.dumps(run(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
