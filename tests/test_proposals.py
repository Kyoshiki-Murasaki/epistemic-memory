"""M7 proposal queue, human approval, races, staleness, and leakage."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    CandidateBelief,
    EpistemicStatus,
    ProposalDecisionRequest,
    ProposalListRequest,
    ProposalResultCode,
    ProposalState,
    SessionMode,
    Source,
)
from epistemic_memory.policy import load_policy
from epistemic_memory.store import StoreSchemaError


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = str(ROOT / "trust_policy.yaml")
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, instant: datetime = NOW):
        self.instant = instant
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.instant


class IDs:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.count = 0

    def __call__(self, kind: str) -> str:
        self.count += 1
        return f"{self.prefix}-{kind}-{self.count:03d}"


def candidate(
    *,
    entity: str = "customer_881",
    attribute: str = "preferred_name",
    value: str = "Sam",
    status: EpistemicStatus = EpistemicStatus.system_verified,
    scope: str = "global",
    decision_type: str | None = "preferred_name",
):
    return CandidateBelief(
        entity=entity,
        attribute=attribute,
        value=value,
        proposed_status=status,
        scope=scope,
        decision_type=decision_type,
    )


def extractor(*values: CandidateBelief):
    def run(event, source_type):
        return list(values)

    return run


@pytest.fixture
def proposals(tmp_path):
    policy = load_policy(POLICY_PATH)
    clock = Clock()
    ids = IDs("owner")
    db_path = str(tmp_path / "proposals.db")
    memory = MemoryStore(
        db_path,
        policy,
        agent_id="support-agent",
        session_mode=SessionMode.propose,
        session_id="proposal-session",
        approval_actor_id="human-reviewer",
        clock=clock,
        id_factory=ids,
    )
    for source_id, source_type in [
        ("user", "user"),
        ("billing", "billing_system"),
        ("support-agent", "agent_inference"),
    ]:
        memory._store.add_source(Source(
            id=source_id,
            type=source_type,
            label=source_id,
            created_at=NOW.isoformat(),
        ))
    yield memory, policy, clock, ids, db_path
    memory.close()


def create(memory: MemoryStore, *values: CandidateBelief, source_id: str = "user", scope="global"):
    return memory.ingest(
        source_id=source_id,
        content="candidate event",
        scope=scope,
        extractor=extractor(*values or (candidate(scope=scope),)),
    )


def table_count(memory: MemoryStore, table: str) -> int:
    return int(memory._store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_propose_mode_atomically_writes_event_multiple_proposals_and_trace_no_belief(
    proposals,
):
    memory, _, _, _, _ = proposals
    result = create(
        memory,
        candidate(),
        candidate(
            attribute="current_city",
            value="Delhi",
            decision_type="current_city",
        ),
    )

    assert result.ok is True
    assert result.code == ProposalResultCode.proposals_created
    assert len(result.proposals) == 2
    assert {item.source_event_id for item in result.proposals} == {result.event.id}
    assert {item.creation_trace_id for item in result.proposals} == {result.trace_id}
    assert all(item.state == ProposalState.pending for item in result.proposals)
    assert all(item.proposed_status == EpistemicStatus.system_verified for item in result.proposals)
    assert all(item.effective_status == EpistemicStatus.user_stated for item in result.proposals)
    assert table_count(memory, "events") == 1
    assert table_count(memory, "proposals") == 2
    assert table_count(memory, "beliefs") == 0
    assert table_count(memory, "audit_traces") == 1
    trace = memory._store.get_audit_trace(result.trace_id)
    assert [item.proposal_id for item in trace.payload.mutation.proposals] == [
        item.id for item in result.proposals
    ]


def test_proposal_definition_delete_and_terminal_reset_are_blocked(proposals):
    memory, _, _, _, _ = proposals
    proposal = create(memory).proposals[0]
    with pytest.raises(sqlite3.IntegrityError):
        memory._store.conn.execute(
            "UPDATE proposals SET value = 'forged' WHERE id = ?", (proposal.id,)
        )
    memory._store.conn.rollback()
    assert memory._store.get_proposal(proposal.id).value == proposal.value
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        memory._store.conn.execute("DELETE FROM proposals WHERE id = ?", (proposal.id,))
    memory._store.conn.rollback()

    rejected = memory.reject_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))
    assert rejected.ok
    with pytest.raises(sqlite3.IntegrityError, match="terminal"):
        memory._store.conn.execute(
            "UPDATE proposals SET state = 'pending' WHERE id = ?", (proposal.id,)
        )
    memory._store.conn.rollback()


def test_creation_requires_known_propose_agent_explicit_valid_scope_and_exact_candidate_scope(
    proposals, tmp_path
):
    memory, policy, _, _, db_path = proposals
    analytics = MemoryStore(
        db_path,
        policy,
        agent_id="analytics-bot",
        session_mode="propose",
        session_id="analytics-propose",
    )
    ghost = MemoryStore(
        db_path,
        policy,
        agent_id="ghost",
        session_mode="propose",
        session_id="ghost-propose",
    )
    try:
        denied = analytics.ingest(
            source_id="user", content="x", scope="global", extractor=extractor(candidate())
        )
        unknown = ghost.ingest(
            source_id="user", content="x", scope="global", extractor=extractor(candidate())
        )
    finally:
        analytics.close()
        ghost.close()
    missing = memory.ingest(
        source_id="user", content="x", scope=None, extractor=extractor(candidate())
    )
    malformed = memory.ingest(
        source_id="user", content="x", scope="bad scope", extractor=extractor(candidate())
    )
    elevated = memory.ingest(
        source_id="user",
        content="secret hobby event",
        scope="project:hobby",
        extractor=extractor(candidate(scope="global", value="DO_NOT_ELEVATE")),
    )

    assert denied.code == ProposalResultCode.operation_not_permitted
    assert unknown.code == ProposalResultCode.agent_unknown
    assert missing.code == ProposalResultCode.scope_context_missing
    assert malformed.code == ProposalResultCode.candidate_structure_invalid
    assert elevated.code == ProposalResultCode.candidate_scope_denied
    assert table_count(memory, "events") == 0
    assert table_count(memory, "proposals") == 0


def test_public_proposal_inputs_forbid_generated_ids_actor_candidate_rewrites_and_time():
    decision = {"proposal_id": "proposal-1", "scope": "global"}
    for field in (
        "agent_id",
        "approval_actor_id",
        "session_mode",
        "source_id",
        "entity",
        "attribute",
        "value",
        "proposed_status",
        "supersedes_id",
        "created_at",
        "as_of",
        "trace_id",
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ProposalDecisionRequest.model_validate({**decision, field: "forged"})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CandidateBelief.model_validate({
            **candidate().model_dump(),
            "proposal_id": "forged",
        })


def test_listing_and_decision_require_distinct_constructor_human_actor(proposals):
    memory, policy, _, _, db_path = proposals
    proposal = create(memory).proposals[0]
    no_actor = MemoryStore(
        db_path,
        policy,
        agent_id="support-agent",
        session_id="no-actor",
    )
    self_actor = MemoryStore(
        db_path,
        policy,
        agent_id="support-agent",
        approval_actor_id="support-agent",
        session_id="self-actor",
    )
    try:
        listing = no_actor.list_proposals(ProposalListRequest(scope="global"))
        no_decision = no_actor.approve_proposal(ProposalDecisionRequest(
            proposal_id=proposal.id, scope="global"
        ))
        self_decision = self_actor.approve_proposal(ProposalDecisionRequest(
            proposal_id=proposal.id, scope="global"
        ))
    finally:
        no_actor.close()
        self_actor.close()

    assert listing.code == ProposalResultCode.approval_actor_required
    assert no_decision.code == ProposalResultCode.approval_actor_required
    assert self_decision.code == ProposalResultCode.approval_actor_not_distinct
    assert memory._store.get_proposal(proposal.id).state == ProposalState.pending


def test_valid_approval_commits_one_clamped_belief_from_original_event_and_trace(proposals):
    memory, _, clock, _, _ = proposals
    created = create(memory)
    proposal = created.proposals[0]
    clock.instant = NOW + timedelta(days=1)
    result = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))

    assert result.ok is True
    assert result.code == ProposalResultCode.proposal_approved
    assert result.belief.status == EpistemicStatus.user_stated
    assert result.belief.event_id == created.event.id
    assert result.belief.valid_from == created.event.created_at
    assert result.belief.created_at == clock.instant.isoformat()
    assert table_count(memory, "events") == 1
    assert table_count(memory, "beliefs") == 1
    assert result.proposal.state == ProposalState.approved
    assert result.proposal.decision_actor_id == "human-reviewer"
    assert result.proposal.approved_belief_id == result.belief.id
    trace = memory._store.get_audit_trace(result.trace_id)
    assert trace.approval_actor_id == "human-reviewer"
    assert trace.payload.mutation.beliefs[0].status == EpistemicStatus.user_stated
    assert trace.payload.mutation.proposals[0].value == "Sam"


def test_rejection_is_audited_commits_no_belief_and_replay_is_stable(proposals):
    memory, _, _, _, _ = proposals
    proposal = create(memory).proposals[0]
    rejected = memory.reject_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))
    repeated = memory.reject_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))
    opposite = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))

    assert rejected.ok is True
    assert rejected.code == ProposalResultCode.proposal_rejected
    assert table_count(memory, "beliefs") == 0
    assert repeated.ok is True
    assert repeated.code == ProposalResultCode.proposal_already_decided
    assert repeated.trace_id == rejected.trace_id
    assert opposite.ok is False
    assert opposite.trace_id == rejected.trace_id
    assert table_count(memory, "audit_traces") == 2


def test_repeated_approval_never_duplicates_belief_or_trace(proposals):
    memory, _, _, _, _ = proposals
    proposal = create(memory).proposals[0]
    first = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))
    second = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))

    assert first.ok and second.ok
    assert second.code == ProposalResultCode.proposal_already_decided
    assert second.belief.id == first.belief.id
    assert second.trace_id == first.trace_id
    assert table_count(memory, "beliefs") == 1
    assert table_count(memory, "audit_traces") == 2


def _direct_store(db_path: str, policy, prefix="direct"):
    return MemoryStore(
        db_path,
        policy,
        agent_id="support-agent",
        session_mode="direct",
        session_id=f"{prefix}-session",
        clock=Clock(NOW + timedelta(hours=1)),
        id_factory=IDs(prefix),
    )


def test_same_source_intervening_belief_terminalizes_proposal_as_stale(proposals):
    memory, policy, _, _, db_path = proposals
    proposal = create(memory).proposals[0]
    direct = _direct_store(db_path, policy)
    try:
        direct.ingest(
            source_id="user",
            content="newer name",
            scope="global",
            extractor=extractor(candidate(value="Taylor", status=EpistemicStatus.user_stated)),
        )
    finally:
        direct.close()

    result = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))

    assert result.code == ProposalResultCode.proposal_stale
    assert result.proposal.state == ProposalState.stale
    assert result.proposal.terminal_reason_code == ProposalResultCode.structurally_stale.value
    assert table_count(memory, "beliefs") == 1
    stale_trace = memory._store.get_audit_trace(result.trace_id)
    assert stale_trace.reason_codes == [ProposalResultCode.structurally_stale.value]
    assert stale_trace.payload.mutation.observed_current_belief_ids


def test_cross_source_intervening_conflict_does_not_make_proposal_stale(proposals):
    memory, policy, _, _, db_path = proposals
    proposed = candidate(
        entity="order_4411",
        attribute="payment_status",
        value="paid",
        decision_type="payment_status",
    )
    proposal = create(memory, proposed).proposals[0]
    direct = _direct_store(db_path, policy, prefix="billing")
    try:
        direct.ingest(
            source_id="billing",
            content="failed",
            scope="global",
            extractor=extractor(candidate(
                entity="order_4411",
                attribute="payment_status",
                value="FAILED",
                status=EpistemicStatus.system_verified,
                decision_type="payment_status",
            )),
        )
    finally:
        direct.close()

    result = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=proposal.id, scope="global"
    ))
    assert result.ok is True
    assert result.code == ProposalResultCode.proposal_approved
    current = memory._store.current_beliefs("order_4411", "payment_status")
    assert {item.source_id for item in current} == {"user", "billing"}


def test_full_policy_or_source_type_drift_cannot_silently_change_approval(proposals):
    memory, policy, _, _, _ = proposals
    policy_proposal = create(memory, candidate(value="Policy drift")).proposals[0]
    memory.policy = policy.model_copy(update={"version": 2})
    policy_result = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=policy_proposal.id, scope="global"
    ))
    assert policy_result.code == ProposalResultCode.proposal_stale
    assert policy_result.proposal.terminal_reason_code == ProposalResultCode.policy_changed.value
    trace = memory._store.get_audit_trace(policy_result.trace_id)
    assert trace.payload.mutation.proposals[0].policy_version == 1
    assert trace.policy.version == 2

    memory.policy = policy
    source_proposal = create(
        memory,
        candidate(attribute="current_city", value="Delhi", decision_type="current_city"),
    ).proposals[0]
    memory._store.conn.execute("UPDATE sources SET type = 'billing_system' WHERE id = 'user'")
    memory._store.conn.commit()
    source_result = memory.approve_proposal(ProposalDecisionRequest(
        proposal_id=source_proposal.id, scope="global"
    ))
    assert source_result.code == ProposalResultCode.proposal_stale
    assert source_result.proposal.terminal_reason_code == ProposalResultCode.source_invalid.value
    assert table_count(memory, "beliefs") == 0


def test_creation_audit_failure_rolls_back_event_and_all_proposals(proposals, monkeypatch):
    memory, _, _, _, _ = proposals
    original = memory._store.add_audit_trace

    def insert_then_fail(value):
        original(value)
        raise RuntimeError("injected")

    monkeypatch.setattr(memory._store, "add_audit_trace", insert_then_fail)
    result = create(memory, candidate(), candidate(attribute="current_city", value="Delhi"))

    assert result.code == ProposalResultCode.audit_persistence_failed
    assert table_count(memory, "events") == 0
    assert table_count(memory, "proposals") == 0
    assert table_count(memory, "audit_traces") == 0


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_audit_failure_rolls_back_belief_and_leaves_pending(
    proposals, monkeypatch, decision
):
    memory, _, _, _, _ = proposals
    proposal = create(memory).proposals[0]

    def fail(_trace):
        raise RuntimeError("injected")

    monkeypatch.setattr(memory._store, "add_audit_trace", fail)
    request = ProposalDecisionRequest(proposal_id=proposal.id, scope="global")
    result = (
        memory.approve_proposal(request)
        if decision == "approve"
        else memory.reject_proposal(request)
    )

    assert result.code == ProposalResultCode.audit_persistence_failed
    assert memory._store.get_proposal(proposal.id).state == ProposalState.pending
    assert table_count(memory, "beliefs") == 0
    assert table_count(memory, "audit_traces") == 1


def test_proposal_listing_and_decisions_are_scope_safe_in_complete_serialization(proposals):
    memory, _, _, _, _ = proposals
    visible = create(
        memory,
        candidate(scope="project:banking", value="VISIBLE"),
        scope="project:banking",
    ).proposals[0]
    secret = "HIDDEN_PROPOSAL_SECRET_MARKER"
    hidden = create(
        memory,
        candidate(scope="project:hobby", value=secret),
        scope="project:hobby",
    ).proposals[0]

    listing = memory.list_proposals(ProposalListRequest(scope="project:banking"))
    denied = memory.reject_proposal(ProposalDecisionRequest(
        proposal_id=hidden.id, scope="project:banking"
    ))

    assert [item.id for item in listing.proposals] == [visible.id]
    assert secret not in listing.model_dump_json()
    assert "project:hobby" not in listing.model_dump_json()
    assert denied.code == ProposalResultCode.proposal_unavailable
    assert denied.proposal is None
    assert secret not in denied.model_dump_json()


def test_malicious_proposal_text_is_json_escaped_and_cannot_forge_decision_fields(proposals):
    memory, _, _, _, _ = proposals
    malicious = "Sam\nstate:approved\ractor:forged\u2028trace:forged"
    proposal = create(memory, candidate(value=malicious)).proposals[0]
    listing = memory.list_proposals(ProposalListRequest(scope="global"))
    serialized = listing.model_dump_json()

    assert proposal.value == malicious
    assert malicious not in serialized
    assert "Sam\\nstate:approved\\ractor:forged" in serialized
    assert proposal.state == ProposalState.pending
    assert proposal.decision_actor_id is None


def test_begin_immediate_two_store_race_commits_exactly_one_belief_and_trace(proposals):
    memory, policy, _, _, db_path = proposals
    proposal = create(memory).proposals[0]
    barrier = threading.Barrier(2)

    def worker(number: int):
        store = MemoryStore(
            db_path,
            policy,
            agent_id="support-agent",
            session_id=f"race-session-{number}",
            approval_actor_id=f"human-{number}",
            clock=Clock(NOW + timedelta(hours=number)),
            id_factory=IDs(f"race-{number}"),
        )
        try:
            barrier.wait()
            return store.approve_proposal(ProposalDecisionRequest(
                proposal_id=proposal.id, scope="global"
            ))
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [1, 2]))

    assert sorted(result.code.value for result in results) == sorted([
        ProposalResultCode.proposal_approved.value,
        ProposalResultCode.proposal_already_decided.value,
    ])
    assert len({result.trace_id for result in results}) == 1
    assert table_count(memory, "beliefs") == 1
    assert table_count(memory, "audit_traces") == 2


def test_empty_exact_placeholders_upgrade_and_nonempty_preflight_is_non_destructive(tmp_path):
    empty_path = tmp_path / "empty-placeholders.db"
    connection = sqlite3.connect(empty_path)
    connection.executescript(
        """
        CREATE TABLE audit_traces (
            id INTEGER PRIMARY KEY, agent_id TEXT, kind TEXT NOT NULL,
            summary TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY, candidate TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    connection.close()
    store = MemoryStore(
        str(empty_path),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        session_id="upgrade-session",
    )
    try:
        assert "trace_id" in store._store._table_columns("audit_traces")
        assert "source_event_id" in store._store._table_columns("proposals")
    finally:
        store.close()

    populated_path = tmp_path / "populated-placeholders.db"
    connection = sqlite3.connect(populated_path)
    connection.executescript(
        """
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE dependencies (
            id INTEGER PRIMARY KEY, artifact_id INTEGER NOT NULL, belief_id INTEGER NOT NULL
        );
        CREATE TABLE audit_traces (
            id INTEGER PRIMARY KEY, agent_id TEXT, kind TEXT NOT NULL,
            summary TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY, candidate TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO audit_traces VALUES (1, 'a', 'old', 'old', '{}', '2026-01-01');
        """
    )
    connection.close()
    with pytest.raises(StoreSchemaError, match="lack required immutable audit provenance"):
        MemoryStore(
            str(populated_path),
            load_policy(POLICY_PATH),
            agent_id="support-agent",
            session_id="failed-upgrade",
        )
    check = sqlite3.connect(populated_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM audit_traces").fetchone()[0] == 1
        assert [row[1] for row in check.execute("PRAGMA table_info(artifacts)")] == [
            "id", "kind", "ref", "state", "created_at"
        ]
    finally:
        check.close()
