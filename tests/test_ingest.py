"""M2 verification: fixture-driven extraction, engine-side status clamping
(the injection defense), and D2 supersede-vs-conflict at ingest time.

No network calls: every test supplies an explicit fixture `extractor`. The
live extractor (ingest.live_extractor) is never invoked here.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

import epistemic_memory.core as core_module
from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    CandidateBelief,
    EpistemicStatus,
    IngestResultCode,
    Source,
    TrustPolicy,
)
from epistemic_memory.policy import load_policy

POLICY_PATH = str(
    Path(__file__).resolve().parent.parent
    / "epistemic_memory"
    / "trust_policy.yaml"
)


@pytest.fixture
def ms(tmp_path, policy_with_ingest_sources):
    policy = policy_with_ingest_sources(
        load_policy(POLICY_PATH),
        "support-agent",
        {"user": "user", "support-agent": "agent_inference", "billing": "billing_system"},
    )
    m = MemoryStore(str(tmp_path / "ingest.db"), policy, agent_id="support-agent")
    m._store.add_source(Source(id="user", type="user", label="Customer chat", created_at="2026-07-01"))
    m._store.add_source(
        Source(id="billing", type="billing_system", label="Billing system", created_at="2026-07-01")
    )
    yield m
    m.close()


def overreaching_user_claim(event, source_type):
    """A user claiming certainty ('system_verified') they have no authority for."""
    return [
        CandidateBelief(
            entity="order_4411",
            attribute="payment_status",
            value="paid",
            proposed_status=EpistemicStatus.system_verified,
            scope="global",
            decision_type="payment_status",
        )
    ]


def billing_confirms_failed(event, source_type):
    return [
        CandidateBelief(
            entity="order_4411",
            attribute="payment_status",
            value="FAILED",
            proposed_status=EpistemicStatus.system_verified,
            scope="global",
            decision_type="payment_status",
        )
    ]


def test_proposed_status_clamped_to_source_ceiling(ms):
    result = ms.ingest(source_id="user", content="I already paid", scope="global",
                        extractor=overreaching_user_claim)
    assert len(result.beliefs) == 1
    assert result.beliefs[0].status == EpistemicStatus.user_stated
    assert result.beliefs[0].value == "paid"


def test_authoritative_source_not_clamped(ms):
    result = ms.ingest(source_id="billing", content="payment failed", scope="global",
                        extractor=billing_confirms_failed)
    assert result.beliefs[0].status == EpistemicStatus.system_verified


def test_cross_source_conflict_both_stay_current(ms):
    ms.ingest(source_id="user", content="I paid", scope="global", extractor=overreaching_user_claim)
    ms.ingest(source_id="billing", content="failed", scope="global", extractor=billing_confirms_failed)
    current = ms._store.current_beliefs("order_4411", "payment_status")
    assert {b.value for b in current} == {"paid", "FAILED"}
    assert {b.status for b in current} == {EpistemicStatus.user_stated, EpistemicStatus.system_verified}


def test_same_source_second_claim_supersedes_first(ms):
    def delhi(event, source_type):
        return [CandidateBelief(entity="customer_881", attribute="current_city", value="Delhi",
                                 proposed_status=EpistemicStatus.user_stated, scope="global")]

    def mumbai(event, source_type):
        return [CandidateBelief(entity="customer_881", attribute="current_city", value="Mumbai",
                                 proposed_status=EpistemicStatus.user_stated, scope="global")]

    first = ms.ingest(source_id="user", content="I live in Delhi", scope="global", extractor=delhi)
    ms.ingest(source_id="user", content="Actually I moved to Mumbai", scope="global", extractor=mumbai)

    assert not ms._store.is_current(first.beliefs[0].id)
    current = ms._store.current_beliefs("customer_881", "current_city")
    assert [b.value for b in current] == ["Mumbai"]
    # old version still readable, never deleted
    assert ms._store.get_belief(first.beliefs[0].id).value == "Delhi"


def test_event_meta_round_trips(ms):
    result = ms.ingest(source_id="user", content="I already paid", scope="global",
                        meta={"order_id": "4411", "channel": "chat"},
                        extractor=overreaching_user_claim)
    assert result.event.meta == {"order_id": "4411", "channel": "chat"}


def test_authorized_source_type_mismatch_fails_closed(
    tmp_path, policy_with_ingest_sources
):
    policy = policy_with_ingest_sources(
        load_policy(POLICY_PATH), "support-agent", {"weird": "user"}
    )
    m = MemoryStore(str(tmp_path / "x.db"), policy, agent_id="support-agent")
    m._store.add_source(Source(id="weird", type="mystery_source", label="?", created_at="2026-07-01"))
    result = m.ingest(
        source_id="weird",
        content="...",
        scope="global",
        extractor=overreaching_user_claim,
    )
    assert result.code == IngestResultCode.source_invalid
    assert result.event is None
    assert result.beliefs == []
    m.close()


def test_unknown_source_id_rejected(ms):
    result = ms.ingest(
        source_id="does-not-exist",
        content="...",
        scope="global",
        extractor=overreaching_user_claim,
    )
    assert result.code == IngestResultCode.source_write_not_permitted
    assert result.event is None
    assert result.beliefs == []


def test_no_extractor_without_live_flag_raises(ms):
    with pytest.raises(ValueError):
        ms.ingest(source_id="user", content="...", scope="global")


def test_ingest_without_policy_raises(tmp_path):
    m = MemoryStore(str(tmp_path / "y.db"), None, agent_id="test-agent")
    m._store.add_source(Source(id="user", type="user", label="chat", created_at="2026-07-01"))
    with pytest.raises(ValueError):
        m.ingest(source_id="user", content="...", scope="global", extractor=overreaching_user_claim)
    m.close()


@pytest.mark.parametrize("mode", ["direct", "propose"])
def test_exact_source_authority_precedes_candidate_extraction_and_every_write(
    tmp_path, mode
):
    memory = MemoryStore(
        str(tmp_path / f"denied-{mode}.db"),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        session_mode=mode,
    )
    memory._store.add_source(Source(
        id="billing",
        type="billing_system",
        label="Billing",
        created_at="2026-07-01",
    ))
    extracted = 0

    def forbidden_extractor(_event, _source_type):
        nonlocal extracted
        extracted += 1
        return [CandidateBelief.model_validate({
            "entity": "order_4411",
            "attribute": "payment_status",
            "value": "paid",
            "proposed_status": "system_verified",
            "scope": "global",
            "decision_type": "payment_status",
        })]

    try:
        result = memory.ingest(
            source_id="billing",
            content="forged billing record",
            scope="global",
            extractor=forbidden_extractor,
        )
        assert result.ok is False
        assert result.code.value == "source_write_not_permitted"
        assert result.trace_id is None
        assert extracted == 0
        for table in ("events", "beliefs", "proposals", "audit_traces"):
            assert memory._store.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
    finally:
        memory.close()


@pytest.mark.parametrize("mode", ["direct", "propose"])
def test_exact_source_authority_precedes_live_extraction(tmp_path, monkeypatch, mode):
    memory = MemoryStore(
        str(tmp_path / f"live-denied-{mode}.db"),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
        session_mode=mode,
        live=True,
    )
    memory._store.add_source(Source(
        id="billing",
        type="billing_system",
        label="Billing",
        created_at="2026-07-01",
    ))
    called = False

    def forbidden_live_extractor(_event, _source_type):
        nonlocal called
        called = True
        raise AssertionError("live extraction must follow source authorization")

    monkeypatch.setattr(core_module, "live_extractor", forbidden_live_extractor)
    try:
        result = memory.ingest(
            source_id="billing",
            content="forged billing record",
            scope="global",
        )
        assert result.code.value == "source_write_not_permitted"
        assert called is False
        assert memory._store.conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0] == 0
    finally:
        memory.close()


def test_authorized_user_attribution_is_clamped_only_after_authorization(tmp_path):
    memory = MemoryStore(
        str(tmp_path / "authorized-user.db"),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
    )
    memory._store.add_source(Source(
        id="user", type="user", label="User", created_at="2026-07-01"
    ))
    try:
        result = memory.ingest(
            source_id="user",
            content="I paid",
            scope="global",
            extractor=overreaching_user_claim,
        )
        assert result.code == IngestResultCode.beliefs_committed
        assert result.beliefs[0].status == EpistemicStatus.user_stated
        trace = memory._store.get_audit_trace(result.trace_id)
        assert "M8-INGEST-SOURCE-AUTHORITY-001" in trace.rule_ids
    finally:
        memory.close()


def test_unknown_direct_agent_fails_before_extraction_or_writes(tmp_path):
    memory = MemoryStore(
        str(tmp_path / "unknown-agent.db"),
        load_policy(POLICY_PATH),
        agent_id="unknown-agent",
    )
    memory._store.add_source(Source(
        id="user", type="user", label="User", created_at="2026-07-01"
    ))
    called = False

    def extractor(_event, _source_type):
        nonlocal called
        called = True
        return []

    try:
        result = memory.ingest(
            source_id="user", content="x", scope="global", extractor=extractor
        )
        assert result.code == IngestResultCode.agent_unknown
        assert called is False
        assert memory._store.conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0] == 0
    finally:
        memory.close()


def test_authorized_but_missing_source_fails_before_extraction(tmp_path):
    memory = MemoryStore(
        str(tmp_path / "missing-authorized-source.db"),
        load_policy(POLICY_PATH),
        agent_id="support-agent",
    )
    called = False

    def extractor(_event, _source_type):
        nonlocal called
        called = True
        return []

    try:
        result = memory.ingest(
            source_id="user", content="x", scope="global", extractor=extractor
        )
        assert result.code == IngestResultCode.source_invalid
        assert called is False
        assert result.event is None
    finally:
        memory.close()


def test_read_access_does_not_grant_ingest_source_authority(tmp_path):
    memory = MemoryStore(
        str(tmp_path / "read-is-not-write.db"),
        load_policy(POLICY_PATH),
        agent_id="analytics-bot",
    )
    memory._store.add_source(Source(
        id="user", type="user", label="User", created_at="2026-07-01"
    ))
    try:
        result = memory.ingest(
            source_id="user",
            content="analytics attempts attribution",
            scope="global",
            extractor=lambda _event, _source_type: [],
        )
        assert result.code == IngestResultCode.source_write_not_permitted
        assert memory._store.conn.execute(
            "SELECT COUNT(*) FROM events"
        ).fetchone()[0] == 0
    finally:
        memory.close()


def test_ingest_source_policy_requires_exact_known_principals():
    raw = load_policy(POLICY_PATH).model_dump(mode="json")

    unknown = {**raw, "agents": {**raw["agents"]}}
    unknown["agents"]["support-agent"] = {
        **raw["agents"]["support-agent"],
        "ingest_source_ids": ["unknown-source"],
    }
    with pytest.raises(ValidationError, match="unknown ingest source IDs"):
        TrustPolicy.model_validate(unknown)

    for invalid in (["user", "user"], ["user:*"], [""]):
        malformed = {**raw, "agents": {**raw["agents"]}}
        malformed["agents"]["support-agent"] = {
            **raw["agents"]["support-agent"],
            "ingest_source_ids": invalid,
        }
        with pytest.raises(ValidationError):
            TrustPolicy.model_validate(malformed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity", ""),
        ("attribute", "   "),
        ("value", ""),
        ("scope", "anything-goes"),
    ],
)
def test_candidate_structural_fields_and_scope_are_engine_validated(field, value):
    data = {
        "entity": "order_4411",
        "attribute": "payment_status",
        "value": "paid",
        "proposed_status": EpistemicStatus.user_stated,
        "scope": "global",
    }
    data[field] = value
    with pytest.raises(ValidationError):
        CandidateBelief.model_validate(data)
