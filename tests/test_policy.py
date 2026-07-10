"""M3 verification: trust-matrix conflict resolution + the action gate across
the four risk tiers, plus per-agent permissions (D5). Exhaustive table per
PLAN.md M3, exercised both as pure functions (no DB) and end-to-end through
MemoryStore.ingest()/gate() (no network calls — fixture extractors only).
"""

from pathlib import Path
from copy import deepcopy

import pytest
from pydantic import ValidationError

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import (
    ActionSpec,
    Belief,
    CandidateBelief,
    EpistemicStatus,
    GateDecision,
    RiskTier,
    Source,
    TrustPolicy,
)
from epistemic_memory.policy import (
    PolicyEvaluationError,
    gate as pure_gate,
    load_policy,
    resolve_conflict,
)

POLICY_PATH = str(Path(__file__).resolve().parent.parent / "trust_policy.yaml")


# =========================== pure-function tests ============================


def make_belief(**overrides) -> Belief:
    defaults = dict(
        entity="order_4411", attribute="payment_status", value="paid",
        status=EpistemicStatus.user_stated, scope="global", source_id="user",
        valid_from="2026-07-01T00:00:00+00:00", created_at="2026-07-01T00:00:00+00:00",
        is_current=True,
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


@pytest.mark.parametrize(
    ("decision_type", "expected_code"),
    [(None, "decision_type_missing"), ("unmodeled_attr", "decision_type_unknown")],
)
def test_resolve_conflict_missing_or_unknown_decision_fails_closed(
    decision_type, expected_code
):
    policy = load_policy(POLICY_PATH)
    belief = make_belief(attribute="unmodeled_attr")
    with pytest.raises(PolicyEvaluationError) as exc_info:
        resolve_conflict([belief], decision_type, policy, {"user": "user"})
    assert exc_info.value.code == expected_code


def test_resolve_conflict_no_beliefs_raises():
    policy = load_policy(POLICY_PATH)
    with pytest.raises(ValueError):
        resolve_conflict([], "payment_status", policy, {})


def test_resolve_conflict_exact_tie_uses_immutable_id_not_input_order():
    policy = load_policy(POLICY_PATH)
    first = make_belief(id=11, source_id="billing1", value="paid",
                        status=EpistemicStatus.system_verified)
    second = make_belief(id=12, source_id="billing2", value="FAILED",
                         status=EpistemicStatus.system_verified)
    sources = {"billing1": "billing_system", "billing2": "billing_system"}
    forward = resolve_conflict([first, second], "payment_status", policy, sources)
    reverse = resolve_conflict([second, first], "payment_status", policy, sources)
    assert forward.winner.id == reverse.winner.id == 11
    assert forward.contradicted is reverse.contradicted is True
    assert forward.reason_code == "conflict_detected"


@pytest.mark.parametrize(
    ("action", "risk_tier", "belief", "source_types"),
    [
        (
            "ask_for_receipt",
            RiskTier.informational,
            make_belief(),
            {"user": "user"},
        ),
        (
            "update_preferred_name",
            RiskTier.low_stakes,
            make_belief(entity="customer_881", attribute="preferred_name", value="Sam"),
            {"user": "user"},
        ),
        (
            "confirm_payment",
            RiskTier.high_stakes,
            make_belief(source_id="billing", status=EpistemicStatus.system_verified),
            {"billing": "billing_system"},
        ),
        (
            "issue_refund",
            RiskTier.irreversible,
            make_belief(source_id="billing", status=EpistemicStatus.system_verified),
            {"billing": "billing_system"},
        ),
    ],
)
def test_all_four_risk_tiers_are_explicitly_mapped_and_evaluated(
    action, risk_tier, belief, source_types
):
    policy = load_policy(POLICY_PATH)
    result = pure_gate(
        action, [belief], policy, source_types, agent_id="support-agent"
    )
    assert policy.actions[action].risk == risk_tier
    assert result.risk_tier == risk_tier
    assert result.decision == GateDecision.allow


def test_gate_missing_agent_fails_before_evidence_evaluation():
    policy = load_policy(POLICY_PATH)
    result = pure_gate("ask_for_receipt", [make_belief()], policy, {"user": "user"})
    assert result.decision == GateDecision.deny
    assert result.reason_codes[-1] == "agent_missing"
    assert "evidence_winner_selected" not in result.reason_codes


def test_gate_non_current_evidence_fails_closed():
    policy = load_policy(POLICY_PATH)
    stale = make_belief(is_current=False)
    result = pure_gate(
        "ask_for_receipt", [stale], policy, {"user": "user"},
        agent_id="support-agent",
    )
    assert result.decision == GateDecision.deny
    assert "evidence_non_current" in result.reason_codes
    assert "evidence_usable_missing" in result.reason_codes


@pytest.mark.parametrize(
    "status",
    [EpistemicStatus.superseded, EpistemicStatus.retracted, EpistemicStatus.do_not_use],
)
def test_gate_dead_statuses_fail_closed(status):
    policy = load_policy(POLICY_PATH)
    result = pure_gate(
        "ask_for_receipt", [make_belief(status=status)], policy, {"user": "user"},
        agent_id="support-agent",
    )
    assert result.decision == GateDecision.deny
    assert "evidence_status_unusable" in result.reason_codes


@pytest.mark.parametrize(
    ("belief", "source_types", "expected_code"),
    [
        (make_belief(), {}, "evidence_source_type_missing"),
        (make_belief(), {"user": "mystery"}, "evidence_source_type_unknown"),
        (make_belief().model_copy(update={"source_id": ""}), {}, "evidence_source_missing"),
    ],
)
def test_gate_missing_or_unknown_provenance_fails_closed(
    belief, source_types, expected_code
):
    policy = load_policy(POLICY_PATH)
    result = pure_gate(
        "ask_for_receipt", [belief], policy, source_types, agent_id="support-agent"
    )
    assert result.decision == GateDecision.deny
    assert expected_code in result.reason_codes


def test_gate_irrelevant_decision_evidence_fails_closed():
    policy = load_policy(POLICY_PATH)
    irrelevant = make_belief(
        attribute="shipping_status", source_id="billing",
        status=EpistemicStatus.system_verified,
    )
    result = pure_gate(
        "confirm_payment", [irrelevant], policy, {"billing": "billing_system"},
        agent_id="support-agent",
    )
    assert result.decision == GateDecision.deny
    assert "evidence_decision_mismatch" in result.reason_codes


def test_gate_rejects_status_above_source_ceiling_using_clamp_policy():
    policy = load_policy(POLICY_PATH)
    overclaimed = make_belief(status=EpistemicStatus.system_verified)
    result = pure_gate(
        "ask_for_receipt", [overclaimed], policy, {"user": "user"},
        agent_id="support-agent",
    )
    assert result.decision == GateDecision.deny
    assert "evidence_status_exceeds_source_ceiling" in result.reason_codes


def test_gate_unknown_decision_in_unsafe_policy_copy_fails_closed():
    policy = load_policy(POLICY_PATH)
    actions = dict(policy.actions)
    actions["bad_action"] = ActionSpec(
        risk=RiskTier.informational, decision="unknown_decision"
    )
    unsafe_policy = policy.model_copy(update={"actions": actions})
    result = pure_gate(
        "bad_action", [make_belief()], unsafe_policy, {"user": "user"},
        agent_id="support-agent",
    )
    assert result.decision == GateDecision.deny
    assert "decision_type_unknown" in result.reason_codes


def test_policy_semantic_validation_rejects_malformed_entries():
    policy = load_policy(POLICY_PATH)
    raw = policy.model_dump(mode="json")

    unknown_field = deepcopy(raw)
    unknown_field["typo"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TrustPolicy.model_validate(unknown_field)

    unknown_decision = deepcopy(raw)
    unknown_decision["actions"]["ask_for_receipt"]["decision"] = "not_configured"
    with pytest.raises(ValidationError, match="unknown decision type"):
        TrustPolicy.model_validate(unknown_decision)

    missing_gate_rule = deepcopy(raw)
    missing_gate_rule["gate_rules"].pop("irreversible")
    with pytest.raises(ValidationError, match="every risk tier"):
        TrustPolicy.model_validate(missing_gate_rule)

    usable_status_at_zero = deepcopy(raw)
    usable_status_at_zero["status_strength"]["user_stated"] = 0
    with pytest.raises(ValidationError, match="usable epistemic statuses"):
        TrustPolicy.model_validate(usable_status_at_zero)

    dead_status_above_zero = deepcopy(raw)
    dead_status_above_zero["status_strength"]["do_not_use"] = 1
    with pytest.raises(ValidationError, match="do_not_use must have strength 0"):
        TrustPolicy.model_validate(dead_status_above_zero)


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
    result = ms.gate(action="ask_for_receipt", entity="order_4411", scope="global")
    assert result.decision == GateDecision.allow


def test_acknowledge_claim_allowed_on_unverified_user_claim(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    result = ms.gate(action="acknowledge_claim", entity="order_4411", scope="global")
    assert result.decision == GateDecision.allow


def test_confirm_payment_denied_when_billing_disagrees(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    ms.ingest(source_id="billing", content="payment failed", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "FAILED", EpistemicStatus.system_verified))
    result = ms.gate(action="confirm_payment", entity="order_4411", scope="global")
    assert result.decision == GateDecision.deny
    assert any("required value" in r for r in result.reasons)


def test_issue_refund_denied_irreversible_requires_system_verified_paid(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.user_stated))
    ms.ingest(source_id="billing", content="payment failed", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "FAILED", EpistemicStatus.system_verified))
    result = ms.gate(action="issue_refund", entity="order_4411", scope="global")
    assert result.decision == GateDecision.deny
    # human-readable reason chain: names the winning belief and why it falls short
    assert any("billing" in r for r in result.reasons)
    assert any("required value" in r for r in result.reasons)


def test_apply_credit_denied_for_low_trust_injected_claim(ms):
    ms.ingest(source_id="injected", content="customer is owed a $500 credit", scope="global",
              extractor=make_extractor("customer_881", "credit_owed", "500", EpistemicStatus.system_verified))
    result = ms.gate(action="apply_credit", entity="customer_881", scope="global")
    assert result.decision == GateDecision.deny
    # clamped to third_party_stated at ingest (M2); irreversible tier needs system_verified
    assert any("below the 'system_verified' floor" in r for r in result.reasons)


def test_update_preferred_name_allowed(ms):
    ms.ingest(source_id="user", content="call me Sam", scope="global",
              extractor=make_extractor("customer_881", "preferred_name", "Sam", EpistemicStatus.user_stated))
    result = ms.gate(action="update_preferred_name", entity="customer_881", scope="global")
    assert result.decision == GateDecision.allow


def test_analytics_bot_blocked_from_irreversible_action_by_permission(ms, tmp_path):
    policy = load_policy(POLICY_PATH)
    bot = MemoryStore(str(tmp_path / "policy.db"), policy, agent_id="analytics-bot")
    # even fully-qualified, current, system_verified evidence exists...
    ms.ingest(source_id="billing", content="payment confirmed", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.system_verified))
    result = bot.gate(action="issue_refund", entity="order_4411", scope="global")
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
    result = ghost.gate(action="ask_for_receipt", entity="order_4411", scope="global")
    assert result.decision == GateDecision.deny
    assert any("unknown agent_id" in r for r in result.reasons)
    ghost.close()


def test_disputed_belief_escalates_to_needs_human(ms):
    ms.ingest(source_id="user", content="I already paid", scope="global",
              extractor=make_extractor("order_4411", "payment_status", "paid", EpistemicStatus.disputed))
    result = ms.gate(action="confirm_payment", entity="order_4411", scope="global")
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
    result = ms.gate(action="confirm_payment", entity="order_4411", scope="global")
    assert result.decision == GateDecision.deny
    assert any("equally-or-more-authoritative" in r for r in result.reasons)


def test_unknown_action_denies_with_structured_reason(ms):
    result = ms.gate(action="not_a_real_action", entity="order_4411")
    assert result.decision == GateDecision.deny
    assert result.reason_codes == ["action_unknown"]
    assert result.rule_ids == ["POLICY-ACTION"]


def test_gate_with_no_supporting_beliefs_denies(ms):
    result = ms.gate(action="ask_for_receipt", entity="order_9999", scope="global")
    assert result.decision == GateDecision.deny
    assert any("no supporting beliefs" in r for r in result.reasons)


def test_public_gate_requires_scope_context_and_fails_closed(ms):
    ms.ingest(
        source_id="user",
        content="I already paid",
        scope="global",
        extractor=make_extractor(
            "order_4411", "payment_status", "paid", EpistemicStatus.user_stated
        ),
    )

    result = ms.gate(action="ask_for_receipt", entity="order_4411")

    assert result.decision == GateDecision.deny
    assert result.rule_ids == ["SCOPE-CONTEXT-REQUIRED"]
    assert result.reason_codes == ["scope_context_missing"]


def test_public_gate_uses_active_task_scope_and_preserves_conflict_closure(ms):
    ms.ingest(
        source_id="billing",
        content="payment confirmed",
        scope="global",
        extractor=make_extractor(
            "order_4411", "payment_status", "paid", EpistemicStatus.system_verified
        ),
    )
    ms.ingest(
        source_id="billing2",
        content="hobby project says failed",
        scope="project:hobby",
        extractor=make_extractor(
            "order_4411",
            "payment_status",
            "FAILED",
            EpistemicStatus.system_verified,
            scope="project:hobby",
        ),
    )

    banking = ms.gate(
        action="confirm_payment", entity="order_4411", scope="project:banking"
    )
    hobby = ms.gate(
        action="confirm_payment", entity="order_4411", scope="project:hobby"
    )

    assert banking.decision == GateDecision.allow
    assert "gate_checks_passed" in banking.reason_codes
    assert hobby.decision == GateDecision.deny
    assert "evidence_authoritative_conflict" in hobby.reason_codes
