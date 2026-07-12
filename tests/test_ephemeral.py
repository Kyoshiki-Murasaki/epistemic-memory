"""M7 ephemeral sessions: read-only SQLite boundary and transient audit traces."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import epistemic_memory.store as store_module
from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    ArtifactRegistrationRequest,
    AssemblyRequest,
    CandidateBelief,
    CommitmentCreateRequest,
    CommitmentListRequest,
    CommitmentTransitionRequest,
    CorrectionRequest,
    DependencyRegistrationRequest,
    ExplainRequest,
    ExplainResultCode,
    OverdueScanRequest,
    ProposalDecisionRequest,
    ProposalListRequest,
    RetrievalRequest,
    Source,
)
from epistemic_memory.policy import load_policy
from epistemic_memory.store import StoreSchemaError


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = str(ROOT / "epistemic_memory" / "trust_policy.yaml")
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
APPLICATION_TABLES = (
    "sources",
    "events",
    "beliefs",
    "commitments",
    "artifacts",
    "dependencies",
    "proposals",
    "audit_traces",
)


class Clock:
    def __init__(self, instant: datetime = NOW):
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant


class IDs:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.count = 0

    def __call__(self, kind: str) -> str:
        self.count += 1
        return f"{self.prefix}-{kind}-{self.count:03d}"


class ConstantIDs:
    def __init__(self, value: str):
        self.value = value

    def __call__(self, _kind: str) -> str:
        return self.value


def candidate(
    *,
    value: str = "paid",
    scope: str = "global",
    entity: str = "order_4411",
    attribute: str = "payment_status",
):
    return CandidateBelief(
        entity=entity,
        attribute=attribute,
        value=value,
        proposed_status="user_stated",
        scope=scope,
        decision_type=attribute,
    )


def extractor(*values: CandidateBelief):
    return lambda _event, _source_type: list(values)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def application_snapshot(path: Path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(path)
    try:
        result = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in APPLICATION_TABLES
        }
        result["beliefs_fts"] = connection.execute(
            "SELECT rowid, entity, attribute, value FROM beliefs_fts ORDER BY rowid"
        ).fetchall()
        return result
    finally:
        connection.close()


def schema_snapshot(connection: sqlite3.Connection) -> list[tuple]:
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()


def make_store(path: Path, *, mode="direct", prefix="seed", approval_actor=None):
    return MemoryStore(
        str(path),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        session_mode=mode,
        session_id=f"{prefix}-session",
        approval_actor_id=approval_actor,
        clock=Clock(),
        id_factory=IDs(prefix),
    )


@pytest.fixture
def seeded_database(tmp_path):
    path = tmp_path / "ephemeral.db"
    direct = make_store(path)
    direct._store.add_source(Source(
        id="user", type="user", label="Customer", created_at=NOW.isoformat()
    ))
    ingested = direct.ingest(
        source_id="user",
        content="I paid",
        scope="global",
        extractor=extractor(candidate()),
    )
    commitment = direct.add_commitment(CommitmentCreateRequest(
        description="Follow up",
        owner="support",
        beneficiary="customer",
        scope="global",
        deadline=NOW + timedelta(days=1),
        preconditions=[],
    ))
    artifact = direct.register_artifact(ArtifactRegistrationRequest(
        kind="output",
        execution_state="not_applicable",
        scope="global",
        label="Answer",
    ))
    dependency = direct.register_dependency(DependencyRegistrationRequest(
        upstream_kind="belief",
        upstream_id=ingested.beliefs[0].id,
        downstream_artifact_id=artifact.artifact.id,
        scope="global",
    ))
    persisted_assembly = direct.assemble(AssemblyRequest(
        entity="order_4411", scope="global", token_budget=2000
    ))
    direct.close()

    proposer = make_store(path, mode="propose", prefix="proposal")
    proposal = proposer.ingest(
        source_id="user",
        content="Call me Sam",
        scope="global",
        extractor=extractor(candidate(
            entity="customer", attribute="preferred_name", value="Sam"
        )),
    ).proposals[0]
    proposer.close()
    return {
        "path": path,
        "belief_id": ingested.beliefs[0].id,
        "commitment_id": commitment.commitment.id,
        "artifact_id": artifact.artifact.id,
        "dependency_id": dependency.dependency.id,
        "proposal_id": proposal.id,
        "persisted_trace_id": persisted_assembly.trace_id,
    }


def test_ephemeral_allows_scoped_reads_gate_listings_and_persisted_explain(
    seeded_database,
):
    seeded = seeded_database
    memory = make_store(
        seeded["path"],
        mode="ephemeral",
        prefix="ephemeral-read",
        approval_actor="human-reviewer",
    )
    try:
        retrieved = memory.retrieve(RetrievalRequest(
            entity="order_4411", scope="global"
        ))
        assembled = memory.assemble(AssemblyRequest(
            entity="order_4411", scope="global", token_budget=2000
        ))
        gated = memory.gate(
            action="issue_refund", entity="order_4411", scope="global"
        )
        commitments = memory.list_commitments(CommitmentListRequest(scope="global"))
        proposals = memory.list_proposals(ProposalListRequest(scope="global"))
        explained = memory.explain(ExplainRequest(
            trace_id=seeded["persisted_trace_id"], scope="global"
        ))
    finally:
        memory.close()

    assert retrieved.authorized and [item.belief.id for item in retrieved.items] == [
        seeded["belief_id"]
    ]
    assert assembled.ok and assembled.trace_persisted is False
    assert gated.ok and gated.trace_persisted is False
    assert [item.id for item in commitments.commitments] == [seeded["commitment_id"]]
    assert [item.id for item in proposals.proposals] == [seeded["proposal_id"]]
    assert explained.authorized and explained.trace.persisted is True


def test_every_durable_mutation_is_structurally_blocked_and_changes_no_rows(
    seeded_database,
):
    seeded = seeded_database
    before = application_snapshot(seeded["path"])
    memory = make_store(
        seeded["path"],
        mode="ephemeral",
        prefix="ephemeral-block",
        approval_actor="human-reviewer",
    )
    try:
        results = {
            "ingest": memory.ingest(
                source_id="user",
                content="new",
                scope="global",
                extractor=extractor(candidate(value="unpaid")),
            ),
            "commitment_create": memory.add_commitment(CommitmentCreateRequest(
                description="Blocked",
                owner="support",
                beneficiary="customer",
                scope="global",
                deadline=NOW + timedelta(days=2),
                preconditions=[],
            )),
            "commitment_transition": memory.transition_commitment(
                CommitmentTransitionRequest(
                    commitment_id=seeded["commitment_id"],
                    target_state="waiting",
                    scope="global",
                )
            ),
            "overdue_scan": memory.surface_overdue(OverdueScanRequest(scope="global")),
            "artifact_register": memory.register_artifact(ArtifactRegistrationRequest(
                kind="output",
                execution_state="not_applicable",
                scope="global",
                label="Blocked",
            )),
            "dependency_register": memory.register_dependency(
                DependencyRegistrationRequest(
                    upstream_kind="belief",
                    upstream_id=seeded["belief_id"],
                    downstream_artifact_id=seeded["artifact_id"],
                    scope="global",
                )
            ),
            "correction": memory.correct(CorrectionRequest(
                belief_id=seeded["belief_id"],
                kind="correction",
                content="Blocked",
                scope="global",
                value="unpaid",
                proposed_status="user_stated",
            )),
            "proposal_approve": memory.approve_proposal(ProposalDecisionRequest(
                proposal_id=seeded["proposal_id"], scope="global"
            )),
            "proposal_reject": memory.reject_proposal(ProposalDecisionRequest(
                proposal_id=seeded["proposal_id"], scope="global"
            )),
        }
        for operation, result in results.items():
            assert result.code.value == "ephemeral_write_blocked", operation
            assert getattr(result, "trace_id", None) is None, operation
    finally:
        memory.close()

    assert application_snapshot(seeded["path"]) == before


def test_transient_traces_are_local_immutable_nonpersistent_and_collision_safe(
    seeded_database,
):
    seeded = seeded_database
    before_audits = application_snapshot(seeded["path"])["audit_traces"]
    memory = make_store(
        seeded["path"], mode="ephemeral", prefix="transient"
    )
    assembled = memory.assemble(AssemblyRequest(
        entity="order_4411", scope="global", token_budget=2000
    ))
    gated = memory.gate(
        action="issue_refund", entity="order_4411", scope="global"
    )
    assert assembled.trace_id != gated.trace_id
    assert assembled.trace_persisted is False and gated.trace_persisted is False
    first = memory.explain(ExplainRequest(trace_id=assembled.trace_id, scope="global"))
    first.trace.result_code = "caller-forged"
    first.trace.payload.assembly.evidence[0].evidence.value = "caller-forged"
    second = memory.explain(ExplainRequest(trace_id=assembled.trace_id, scope="global"))
    assert second.trace.result_code == "context_assembled"
    assert second.trace.payload.assembly.evidence[0].evidence.value == "paid"
    memory.close()

    fresh = make_store(seeded["path"], mode="ephemeral", prefix="fresh")
    try:
        missing = fresh.explain(ExplainRequest(
            trace_id=assembled.trace_id, scope="global"
        ))
    finally:
        fresh.close()
    assert missing.code == ExplainResultCode.trace_unavailable
    assert application_snapshot(seeded["path"])["audit_traces"] == before_audits

    persisted_collision = MemoryStore(
        str(seeded["path"]),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        session_mode="ephemeral",
        session_id="persisted-collision-session",
        clock=Clock(),
        id_factory=ConstantIDs(seeded["persisted_trace_id"]),
    )
    try:
        rejected = persisted_collision.assemble(AssemblyRequest(
            entity="order_4411", scope="global", token_budget=2000
        ))
        assert rejected.ok is False
        assert rejected.result_code.value == "audit_persistence_failed"
        assert persisted_collision._transient_traces == {}
    finally:
        persisted_collision.close()

    transient_collision = MemoryStore(
        str(seeded["path"]),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        session_mode="ephemeral",
        session_id="transient-collision-session",
        clock=Clock(),
        id_factory=ConstantIDs("same-transient-trace"),
    )
    try:
        assert transient_collision.assemble(AssemblyRequest(
            entity="order_4411", scope="global", token_budget=2000
        )).ok
        duplicate = transient_collision.gate(
            action="issue_refund", entity="order_4411", scope="global"
        )
        assert duplicate.ok is False
        assert duplicate.result_code.value == "audit_persistence_failed"
    finally:
        transient_collision.close()


def test_ephemeral_connection_uses_mode_ro_query_only_and_sqlite_rejects_writes(
    seeded_database, monkeypatch
):
    calls = []
    original_connect = store_module.sqlite3.connect

    def recording_connect(database, *args, **kwargs):
        calls.append((database, kwargs.copy()))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", recording_connect)
    memory = make_store(seeded_database["path"], mode="ephemeral", prefix="ro")
    try:
        assert memory._store.read_only is True
        assert memory._store.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            memory._store.conn.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?)",
                ("blocked", "user", "blocked", NOW.isoformat()),
            )
    finally:
        memory.close()

    assert len(calls) == 1
    database, kwargs = calls[0]
    assert database.endswith("?mode=ro")
    assert database.startswith("file://")
    assert kwargs["uri"] is True


def test_ephemeral_startup_skips_schema_upgrade_placeholder_and_fts_rebuild(
    seeded_database, monkeypatch
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("write-capable startup path reached")

    monkeypatch.setattr(store_module.Store, "_prepare_m6_tables", forbidden)
    monkeypatch.setattr(store_module.Store, "_prepare_m7_tables", forbidden)
    before = application_snapshot(seeded_database["path"])
    before_version = sqlite3.connect(seeded_database["path"]).execute(
        "PRAGMA user_version"
    ).fetchone()[0]
    memory = make_store(seeded_database["path"], mode="ephemeral", prefix="startup")
    try:
        assert memory._store.conn.total_changes == 0
    finally:
        memory.close()
    check = sqlite3.connect(seeded_database["path"])
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == before_version == 7
    finally:
        check.close()
    assert application_snapshot(seeded_database["path"]) == before


def test_missing_ephemeral_database_fails_without_creating_file(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(StoreSchemaError, match="existing file-backed"):
        make_store(missing, mode="ephemeral", prefix="missing")
    assert not missing.exists()


@pytest.mark.parametrize("kind", ["invalid", "incomplete", "wrong_version", "placeholder"])
def test_invalid_ephemeral_databases_fail_without_repair_or_mutation(tmp_path, kind):
    path = tmp_path / f"{kind}.db"
    if kind == "invalid":
        path.write_bytes(b"not a sqlite database")
    else:
        connection = sqlite3.connect(path)
        if kind == "incomplete":
            connection.execute("PRAGMA user_version=7")
            connection.execute("CREATE TABLE sources(id TEXT PRIMARY KEY)")
        elif kind == "wrong_version":
            connection.execute("PRAGMA user_version=6")
            connection.execute("CREATE TABLE marker(value TEXT)")
        else:
            connection.executescript(
                """
                PRAGMA user_version=6;
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
        connection.commit()
        connection.close()
    before = file_hash(path)
    before_size = path.stat().st_size

    with pytest.raises(StoreSchemaError):
        make_store(path, mode="ephemeral", prefix=kind)

    assert file_hash(path) == before
    assert path.stat().st_size == before_size


def test_request_models_cannot_override_host_identity_mode_ids_or_time():
    model_payloads = [
        (CandidateBelief, candidate().model_dump(), {"proposal_id", "trace_id", "created_at"}),
        (ArtifactRegistrationRequest, {
            "kind": "output", "execution_state": "not_applicable",
            "scope": "global", "label": "x",
        }, {"trace_id", "created_at"}),
        (DependencyRegistrationRequest, {
            "upstream_kind": "belief", "upstream_id": 1,
            "downstream_artifact_id": 2, "scope": "global",
        }, {"trace_id", "created_at"}),
        (CorrectionRequest, {
            "belief_id": 1, "kind": "correction", "content": "x",
            "scope": "global", "value": "y", "proposed_status": "user_stated",
        }, {"trace_id", "as_of", "created_at"}),
        (CommitmentCreateRequest, {
            "description": "x", "owner": "o", "beneficiary": "b",
            "scope": "global", "deadline": NOW + timedelta(days=1),
        }, {"trace_id", "created_at", "updated_at"}),
        (CommitmentTransitionRequest, {
            "commitment_id": 1, "target_state": "waiting", "scope": "global",
        }, {"trace_id", "as_of", "updated_at"}),
        (ProposalDecisionRequest, {
            "proposal_id": "proposal-1", "scope": "global",
        }, {"trace_id", "decided_at"}),
        (ExplainRequest, {"trace_id": "trace-1", "scope": "global"}, {"as_of"}),
    ]
    common = {"agent_id", "session_id", "session_mode", "approval_actor_id"}
    for model, payload, model_specific in model_payloads:
        for field in common | model_specific:
            with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
                model.model_validate({**payload, field: "caller-controlled"})


def test_delete_journal_ephemeral_session_preserves_file_hash_and_application_rows(
    seeded_database,
):
    path = seeded_database["path"]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        connection.close()
    before_hash = file_hash(path)
    before = application_snapshot(path)

    memory = make_store(path, mode="ephemeral", prefix="delete-journal")
    try:
        assert memory.retrieve(RetrievalRequest(scope="global")).authorized
        assert memory.assemble(AssemblyRequest(
            scope="global", token_budget=2000
        )).trace_persisted is False
        assert memory.gate(
            action="issue_refund", entity="order_4411", scope="global"
        ).trace_persisted is False
    finally:
        memory.close()

    assert application_snapshot(path) == before
    assert file_hash(path) == before_hash
    assert not Path(f"{path}-journal").exists()


def test_wal_ephemeral_session_allows_sidecars_but_changes_no_durable_state(tmp_path):
    path = tmp_path / "wal.db"
    direct = make_store(path, prefix="wal-seed")
    direct.close()
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?)",
            ("user", "user", "Customer", NOW.isoformat()),
        )
        writer.commit()
        before_rows = {
            table: writer.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in APPLICATION_TABLES
        }
        before_schema = schema_snapshot(writer)
        before_version = writer.execute("PRAGMA user_version").fetchone()[0]
        before_audits = writer.execute("SELECT * FROM audit_traces").fetchall()
        wal_path = Path(f"{path}-wal")
        shm_path = Path(f"{path}-shm")
        assert wal_path.exists() and shm_path.exists()

        memory = make_store(path, mode="ephemeral", prefix="wal-read")
        try:
            assert memory.retrieve(RetrievalRequest(scope="global")).authorized
            transient = memory.assemble(AssemblyRequest(scope="global", token_budget=2000))
            assert transient.trace_persisted is False
        finally:
            memory.close()

        assert {
            table: writer.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in APPLICATION_TABLES
        } == before_rows
        assert schema_snapshot(writer) == before_schema
        assert writer.execute("PRAGMA user_version").fetchone()[0] == before_version == 7
        assert writer.execute("SELECT * FROM audit_traces").fetchall() == before_audits
        assert wal_path.exists() and shm_path.exists()
    finally:
        writer.close()


def test_open_close_empty_ephemeral_session_creates_no_application_or_fts_rows(tmp_path):
    path = tmp_path / "empty.db"
    direct = make_store(path, prefix="empty-seed")
    direct.close()
    memory = make_store(path, mode="ephemeral", prefix="empty-session")
    memory.close()

    snapshot = application_snapshot(path)
    assert all(snapshot[table] == [] for table in APPLICATION_TABLES)
    assert snapshot["beliefs_fts"] == []
