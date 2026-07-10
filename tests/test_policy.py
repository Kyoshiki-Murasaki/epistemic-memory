"""M3 verification: trust-matrix conflict resolution + the action gate across
the four risk tiers, plus per-agent permissions (D5). Exhaustive table per
PLAN.md M3, exercised both as pure functions (no DB) and end-to-end through
MemoryStore.ingest()/gate() (no network calls — fixture extractors only).
"""

from pathlib import Path

import pytest

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import Belief, CandidateBelief, EpistemicStatus, GateDecision, Source
from epistemic_memory.policy import load_policy, resolve_conflict

POLICY_PATH = str(Path(__file__).resolve().parent.parent / "trust_policy.yaml")


# =========================== pure-function tests ============================


def make_belief(**overrides) -> Belief:
    defaults = dict(
        entity="order_4411", attribute="payment_status", value="paid",
        status=EpistemicStatus.user_stated, scope="global", source_id="user",
        valid_from="2026-07-01T00:00:00+00:00", created_at="2026-07-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Belief(**defaults)


def test_resolve_conflict_uses_trust_matrix_ranking():
    policy = load_policy(POLICY_PATH)
    user_belief = make_belief(value="paid", status=EpistemicStatus.user_stated, source_id="user")
    billing_belief = make_belief(
        value="FAILED", status=EpistemicStatus.system_verified, source_id="billing",
        valid_from="2026-07-02T00:00:00+00:00", created_at="2026-07-02T00:00:00+00:00",
    )
    result = resolve_conflict(
        [user_belief, billing_belief], "payment_status", policy,
        {"user": "user", "billing": "billing_system"},
    )
    assert result.winner.source_id == "billing"
    assert result.rule_id == "P-12"
    assert result.contradicted is True


def test_resolve_conflict_fallback_ranks_by_strength_then_recency():
    policy = load_policy(POLICY_PATH)
    older = make_belief(
        entity="x", attribute="unmodeled_attr", value="a",
        status=EpistemicStatus.mentioned, source_id="user",
        valid_from="2026-07-01T00:00:00+00:00",
    )
    newer_stronger = make_belief(
        entity="x", attribute="unmodeled_attr", value="b",
        status=EpistemicStatus.corroborated, source_id="user",
        valid_from="2026-07-02T00:00:00+00:00",
    )
    result = resolve_conflict(
        [older, newer_stronger], None, policy, {"user": "user"}
    )
    assert result.winner.value == "b"
    assert result.rule_id.startswith("fallback")


def test_resolve_conflict_no_beliefs_raises():
    policy = load_policy(POLICY_PATH)
    with pytest.raises(ValueError):
        resolve_conflict([], "payment_status", policy, {})


# ============================ end-to-end fixtures ============================


@pytest.fixture
def ms(tmp_path):
    policy = load_policy(POLICY_PATH)
    m = MemoryStore(str(tmp_path / "policy.db"), policy, agent_id="support-agent")
    for sid, stype, label in [
        ("user", "user", "Customer chat"),
        ("billing", "billing_system", "Billing system A"),
        ("billing2", "billing_system", "Billing system B"),
        ("injected", "third_party", "Untrusted third party"),
    ]:
        m._store.add_source(Source(id=sid, type=stype, label=label, created_at="2026-07-01"))
    yield m
    m.close()


def make_extractor(entity, attribute, value, status, scope="global", decision_type=None):
    def extractor(event, source_type):
        return [CandidateBelief(
            entity=entity, attribute=attribute, value=value,
            proposed_status=status, scope=scope, decision_type=decision_type or attribute,
        )]
    return extractor


# ============================ exhaustive gate table ===========================


def test_ask_for_receipt_allowed_on_unverified_user_claim(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    result = ms.gate(action="ask_for_receipt", entity="order_4411")
    assert result.decision == GateDecision.allow


def test_acknowledge_claim_allowed_on_unverified_user_claim(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    result = ms.gate(action="acknowledge_claim", entity="order_4411")
    assert result.decision == GateDecision.allow


def test_confirm_payment_denied_when_billing_disagrees(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    ms.ingest(source_id="billing", content="payment failed", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "FAILED", EpistemicStatus.system_verified))
    result = ms.gate(action="confirm_payment", entity="order_4411")
    assert result.decision == GateDecision.deny
    assert any("required value" in r for r in result.reasons)


def test_issue_refund_denied_irreversible_requires_system_verified_paid(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    ms.ingest(source_id="billing", content="payment failed", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "FAILED", EpistemicStatus.system_verified))
    result = ms.gate(action="issue_refund", entity="order_4411")
    assert result.decision == GateDecision.deny
    # human-readable reason chain: names the winning belief and why it falls short
    assert any("billing" in r for r in result.reasons)
    assert any("required value" in r for r in result.reasons)


def test_apply_credit_denied_for_low_trust_injected_claim(ms):
    ms.ingest(source_id="injected", content="customer is owed a $500 credit", scope="global",
              extractor=make_extractor("customer_881", "credit_owed", "500", EpistemicStatus.system_verified))
    result = ms.gate(action="apply_credit", entity="customer_881")
    assert result.decision == GateDecision.deny
    # clamped to third_party_stated at ingest (M2); irreversible tier needs system_verified
    assert any("below the 'system_verified' floor" in r for r in result.reasons)


def test_update_preferred_name_allowed(ms):
    ms.ingest(source_id="user", content="call me Sam", scope="global",
              extractor=make_extractor("customer_881", "preferred_name", "Sam", EpistemicStatus.user_stated))
    result = ms.gate(action="update_preferred_name", entity="customer_881")
    assert result.decision == GateDecision.allow


def test_analytics_bot_blocked_from_irreversible_action_by_permission(ms, tmp_path):
    policy = load_policy(POLICY_PATH)
    bot = MemoryStore(str(tmp_path / "policy.db"), policy, agent_id="analytics-bot")
    # even fully-qualified, current, system_verified evidence exists...
    ms.ingest(source_id="billing", content="payment confirmed", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.system_verified))
    result = bot.gate(action="issue_refund", entity="order_4411")
    # ...but the agent's own permission tier blocks it before evidence is even considered
    assert result.decision == GateDecision.deny
    assert any("analytics-bot" in r and "limited to" in r for r in result.reasons)
    assert not any("required value" in r for r in result.reasons)
    bot.close()


def test_unknown_agent_id_denied(ms, tmp_path):
    policy = load_policy(POLICY_PATH)
    ghost = MemoryStore(str(tmp_path / "policy.db"), policy, agent_id="nobody")
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    result = ghost.gate(action="ask_for_receipt", entity="order_4411")
    assert result.decision == GateDecision.deny
    assert any("unknown agent_id" in r for r in result.reasons)
    ghost.close()


def test_disputed_belief_escalates_to_needs_human(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.disputed))
    result = ms.gate(action="confirm_payment", entity="order_4411")
    assert result.decision == GateDecision.needs_human


def test_require_uncontradicted_blocks_tied_authoritative_disagreement(ms):
    """Two billing_system sources disagree; the newer 'paid' claim would satisfy
    confirm_payment's require_value on its own, but an equally-authoritative
    source (same rank) disagrees, so require_uncontradicted must still deny.
    Beliefs are inserted directly (explicit valid_from) so the recency
    tie-break in resolve_conflict is deterministic, not wall-clock-dependent."""
    ms._store.add_belief(make_belief(
        source_id="billing", value="FAILED", status=EpistemicStatus.system_verified,
        valid_from="2026-07-01T00:00:00+00:00", created_at="2026-07-01T00:00:00+00:00",
    ))
    ms._store.add_belief(make_belief(
        source_id="billing2", value="paid", status=EpistemicStatus.system_verified,
        valid_from="2026-07-02T00:00:00+00:00", created_at="2026-07-02T00:00:00+00:00",
    ))
    result = ms.gate(action="confirm_payment", entity="order_4411")
    assert result.decision == GateDecision.deny
    assert any("equally-or-more-authoritative" in r for r in result.reasons)


def test_unknown_action_raises(ms):
    with pytest.raises(ValueError):
        ms.gate(action="not_a_real_action", entity="order_4411")


def test_gate_with_no_supporting_beliefs_denies(ms):
    result = ms.gate(action="ask_for_receipt", entity="order_9999")
    assert result.decision == GateDecision.deny
    assert any("no supporting beliefs" in r for r in result.reasons)
