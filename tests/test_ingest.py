"""M2 verification: fixture-driven extraction, engine-side status clamping
(the injection defense), and D2 supersede-vs-conflict at ingest time.

No network calls: every test supplies an explicit fixture `extractor`. The
live extractor (ingest.live_extractor) is never invoked here.
"""

from pathlib import Path

import pytest

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import CandidateBelief, EpistemicStatus, Source
from epistemic_memory.policy import load_policy

POLICY_PATH = str(Path(__file__).resolve().parent.parent / "trust_policy.yaml")


@pytest.fixture
def ms(tmp_path):
    policy = load_policy(POLICY_PATH)
    m = MemoryStore(str(tmp_path / "ingest.db"), policy, agent_id="test-agent")
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


def test_unknown_source_type_rejected(tmp_path):
    policy = load_policy(POLICY_PATH)
    m = MemoryStore(str(tmp_path / "x.db"), policy, agent_id="test-agent")
    m._store.add_source(Source(id="weird", type="mystery_source", label="?", created_at="2026-07-01"))
    with pytest.raises(ValueError):
        m.ingest(source_id="weird", content="...", scope="global", extractor=overreaching_user_claim)
    m.close()


def test_unknown_source_id_rejected(ms):
    with pytest.raises(ValueError):
        ms.ingest(source_id="does-not-exist", content="...", scope="global",
                   extractor=overreaching_user_claim)


def test_no_extractor_without_live_flag_raises(ms):
    with pytest.raises(ValueError):
        ms.ingest(source_id="user", content="...", scope="global")


def test_ingest_without_policy_raises(tmp_path):
    m = MemoryStore(str(tmp_path / "y.db"), None, agent_id="test-agent")
    m._store.add_source(Source(id="user", type="user", label="chat", created_at="2026-07-01"))
    with pytest.raises(ValueError):
        m.ingest(source_id="user", content="...", scope="global", extractor=overreaching_user_claim)
    m.close()
