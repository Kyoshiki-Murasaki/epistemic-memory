"""Pure trust-policy evaluation plus the YAML loader.

The loader performs I/O once. ``resolve_conflict`` and ``gate`` receive typed
policy data and evidence and never access storage or mutate their inputs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import yaml

from .models import (
    Belief,
    ConflictResolution,
    EpistemicStatus,
    GateDecision,
    GateResult,
    PolicyReason,
    RiskTier,
    TrustPolicy,
)


class PolicyEvaluationError(ValueError):
    """Deterministic, machine-readable failure from pure policy evaluation."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def load_policy(path: str) -> TrustPolicy:
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError("policy document must be a YAML mapping")
    return TrustPolicy.model_validate(raw)


def clamp_status(
    proposed: EpistemicStatus, source_type: str, policy: TrustPolicy
) -> EpistemicStatus:
    """Clamp an LLM-proposed status to the configured source ceiling."""
    if source_type not in policy.source_status_ceiling:
        raise ValueError(f"unknown source type (not in trust_policy.yaml): {source_type!r}")
    ceiling = policy.source_status_ceiling[source_type]
    if policy.status_strength[proposed.value] > policy.status_strength[ceiling.value]:
        return ceiling
    return proposed


def _rank(source_type: str, ranking: list[str]) -> int:
    return ranking.index(source_type) if source_type in ranking else len(ranking)


def _recency(belief: Belief) -> float:
    try:
        parsed = datetime.fromisoformat(belief.valid_from)
    except ValueError as exc:
        raise PolicyEvaluationError(
            "evidence_invalid_timestamp",
            f"belief {belief.id!r} has invalid valid_from timestamp {belief.valid_from!r}",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _stable_belief_key(belief: Belief) -> tuple:
    """Immutable final tie-break independent of input or SQLite row order."""
    if belief.id is not None:
        return (0, belief.id)
    return (
        1,
        belief.entity,
        belief.attribute,
        belief.value,
        belief.status.value,
        belief.scope,
        belief.source_id,
        belief.event_id if belief.event_id is not None else -1,
        belief.valid_from,
        belief.created_at,
    )


def _source_type(
    belief: Belief, source_types: dict[str, str], policy: TrustPolicy
) -> str:
    if not belief.source_id.strip():
        raise PolicyEvaluationError(
            "evidence_source_missing", f"belief {belief.id!r} has no source identity"
        )
    source_type = source_types.get(belief.source_id)
    if not source_type:
        raise PolicyEvaluationError(
            "evidence_source_type_missing",
            f"source type is missing for belief {belief.id!r} source {belief.source_id!r}",
        )
    if source_type not in policy.source_status_ceiling:
        raise PolicyEvaluationError(
            "evidence_source_type_unknown",
            f"source type {source_type!r} is not configured in policy",
        )
    return source_type


def resolve_conflict(
    beliefs: list[Belief],
    decision_type: Optional[str],
    policy: TrustPolicy,
    source_types: dict[str, str],
) -> ConflictResolution:
    """Rank one structural conflict without deleting or muting any side."""
    if not beliefs:
        raise PolicyEvaluationError(
            "conflict_evidence_missing", "resolve_conflict requires at least one belief"
        )
    if not decision_type:
        raise PolicyEvaluationError(
            "decision_type_missing", "conflict resolution requires a decision type"
        )
    rule = policy.trust_matrix.get(decision_type)
    if rule is None:
        raise PolicyEvaluationError(
            "decision_type_unknown", f"unknown decision type: {decision_type!r}"
        )
    if len({belief.key for belief in beliefs}) != 1:
        raise PolicyEvaluationError(
            "conflict_mixed_keys", "conflict evidence must share one (entity, attribute) key"
        )
    if any(
        belief.attribute != decision_type
        or belief.decision_type not in (None, decision_type)
        for belief in beliefs
    ):
        raise PolicyEvaluationError(
            "conflict_decision_mismatch",
            f"conflict evidence does not support decision type {decision_type!r}",
        )

    source_by_object = {
        id(belief): _source_type(belief, source_types, policy) for belief in beliefs
    }

    def sort_key(belief: Belief) -> tuple:
        return (
            _rank(source_by_object[id(belief)], rule.ranking),
            -policy.status_strength[belief.status.value],
            -_recency(belief),
            _stable_belief_key(belief),
        )

    ordered = sorted(beliefs, key=sort_key)
    winner, losers = ordered[0], ordered[1:]
    contradicted = any(belief.value != winner.value for belief in losers)
    return ConflictResolution(
        winner=winner,
        losers=losers,
        rule_id=rule.rule_id,
        contradicted=contradicted,
        reason_code="conflict_detected" if contradicted else "policy_ranked",
    )


def _reason(code: str, message: str, rule_id: Optional[str]) -> PolicyReason:
    return PolicyReason(code=code, message=message, rule_id=rule_id)


def _result(
    decision: GateDecision,
    action: str,
    decision_type: Optional[str],
    risk_tier: Optional[RiskTier],
    details: list[PolicyReason],
) -> GateResult:
    return GateResult(
        decision=decision,
        action=action,
        decision_type=decision_type,
        risk_tier=risk_tier,
        rule_ids=list(
            dict.fromkeys(detail.rule_id for detail in details if detail.rule_id is not None)
        ),
        reason_codes=[detail.code for detail in details],
        reasons=[detail.message for detail in details],
        details=details,
    )


def _evidence_problem(
    belief: Belief,
    decision_type: str,
    policy: TrustPolicy,
    source_types: dict[str, str],
    rule_id: str,
) -> Optional[PolicyReason]:
    if belief.is_current is not True:
        return _reason(
            "evidence_non_current",
            f"belief {belief.id!r} is not structurally current",
            rule_id,
        )
    if belief.status in {
        EpistemicStatus.superseded,
        EpistemicStatus.retracted,
        EpistemicStatus.do_not_use,
    }:
        return _reason(
            "evidence_status_unusable",
            f"belief {belief.id!r} status '{belief.status.value}' is no longer usable",
            rule_id,
        )
    if belief.attribute != decision_type or belief.decision_type not in (None, decision_type):
        return _reason(
            "evidence_decision_mismatch",
            f"belief {belief.id!r} does not support decision type {decision_type!r}",
            rule_id,
        )
    try:
        source_type = _source_type(belief, source_types, policy)
    except PolicyEvaluationError as exc:
        return _reason(exc.code, str(exc), rule_id)
    if clamp_status(belief.status, source_type, policy) != belief.status:
        return _reason(
            "evidence_status_exceeds_source_ceiling",
            f"belief {belief.id!r} status '{belief.status.value}' exceeds the "
            f"configured ceiling for source type '{source_type}'",
            rule_id,
        )
    return None


def gate(
    action: str,
    supporting_beliefs: list[Belief],
    policy: TrustPolicy,
    source_types: dict[str, str],
    *,
    agent_id: Optional[str] = None,
) -> GateResult:
    """Evaluate an action without I/O; all missing/invalid inputs fail closed."""
    action_spec = policy.actions.get(action)
    if action_spec is None:
        return _result(
            GateDecision.deny,
            action,
            None,
            None,
            [_reason("action_unknown", f"unknown action: {action!r}", "POLICY-ACTION")],
        )

    decision_type = action_spec.decision
    risk_tier = action_spec.risk
    rule = policy.gate_rules.get(risk_tier)
    if rule is None:
        return _result(
            GateDecision.deny,
            action,
            decision_type,
            risk_tier,
            [_reason(
                "gate_rule_missing",
                f"no gate rule is configured for tier {risk_tier.value!r}",
                "POLICY-GATE",
            )],
        )

    details = [_reason(
        "action_policy_selected",
        f"action '{action}' is tier '{risk_tier.value}' (decision_type={decision_type})",
        rule.rule_id,
    )]

    def add(code: str, message: str, rule_id: Optional[str] = None) -> None:
        details.append(_reason(code, message, rule_id or rule.rule_id))

    def finish(decision: GateDecision) -> GateResult:
        return _result(decision, action, decision_type, risk_tier, details)

    if decision_type not in policy.trust_matrix:
        add("decision_type_unknown", f"unknown decision type: {decision_type!r}")
        return finish(GateDecision.deny)

    # Permission ceilings must run before any evidence inspection.
    if agent_id is None:
        add("agent_missing", "agent_id is required; missing agents fail closed", "POLICY-AGENT")
        return finish(GateDecision.deny)
    agent_permissions = policy.agents.get(agent_id)
    if agent_permissions is None:
        add("agent_unknown", f"unknown agent_id: {agent_id!r}", "POLICY-AGENT")
        return finish(GateDecision.deny)
    if policy.risk_tiers.index(risk_tier) > policy.risk_tiers.index(
        agent_permissions.max_action_tier
    ):
        add(
            "agent_tier_exceeded",
            f"agent '{agent_id}' is limited to "
            f"'{agent_permissions.max_action_tier.value}' tier; "
            f"'{action}' requires '{risk_tier.value}'",
            "POLICY-AGENT",
        )
        return finish(GateDecision.deny)

    if not supporting_beliefs:
        add("evidence_missing", "no supporting beliefs for this decision")
        return finish(GateDecision.deny)

    usable: list[Belief] = []
    for belief in supporting_beliefs:
        problem = _evidence_problem(
            belief, decision_type, policy, source_types, rule.rule_id
        )
        if problem is None:
            usable.append(belief)
        else:
            details.append(problem)
    if not usable:
        add(
            "evidence_usable_missing",
            "no current, relevant, provenance-backed supporting beliefs remain",
        )
        return finish(GateDecision.deny)
    if len({belief.key for belief in usable}) != 1:
        add("evidence_mixed_keys", "supporting beliefs span multiple structural keys")
        return finish(GateDecision.deny)

    try:
        conflict = resolve_conflict(usable, decision_type, policy, source_types)
    except PolicyEvaluationError as exc:
        add(exc.code, str(exc))
        return finish(GateDecision.deny)

    winner = conflict.winner
    add(
        "evidence_winner_selected",
        f"winning belief: {winner.entity}.{winner.attribute}={winner.value!r} "
        f"[{winner.status.value} · source={winner.source_id}] (rule {conflict.rule_id})",
        conflict.rule_id,
    )
    if conflict.contradicted:
        disagreeing = [belief for belief in conflict.losers if belief.value != winner.value]
        add(
            "evidence_conflict_detected",
            "conflicting belief(s): "
            + ", ".join(
                f"{belief.value!r} [{belief.status.value} · source={belief.source_id}]"
                for belief in disagreeing
            ),
            conflict.rule_id,
        )

    if winner.status == EpistemicStatus.disputed and risk_tier != RiskTier.informational:
        add(
            "evidence_disputed",
            "winning belief is disputed; escalating to human review rather than auto-deciding",
        )
        return finish(GateDecision.needs_human)
    if policy.status_strength[winner.status.value] < policy.status_strength[rule.min_status.value]:
        add(
            "evidence_below_status_floor",
            f"status '{winner.status.value}' is below the '{rule.min_status.value}' floor "
            f"required for '{risk_tier.value}' actions",
        )
        return finish(GateDecision.deny)

    trust_rule = policy.trust_matrix[decision_type]
    winner_source = _source_type(winner, source_types, policy)
    if rule.require_authoritative_source and winner_source not in trust_rule.authoritative:
        add(
            "evidence_source_not_authoritative",
            f"source '{winner_source}' is not authoritative for '{decision_type}' "
            f"(authoritative: {trust_rule.authoritative})",
            trust_rule.rule_id,
        )
        return finish(GateDecision.deny)
    if rule.require_uncontradicted:
        winner_rank = _rank(winner_source, trust_rule.ranking)
        equal_or_higher_disagrees = any(
            _rank(_source_type(belief, source_types, policy), trust_rule.ranking) <= winner_rank
            and belief.value != winner.value
            for belief in conflict.losers
        )
        if equal_or_higher_disagrees:
            add(
                "evidence_authoritative_conflict",
                "an equally-or-more-authoritative source disagrees with the winning belief",
                trust_rule.rule_id,
            )
            return finish(GateDecision.deny)
    if action_spec.require_value is not None and winner.value != action_spec.require_value:
        add(
            "evidence_required_value_missing",
            f"required value '{action_spec.require_value}' but the winning belief asserts "
            f"'{winner.value}'",
        )
        return finish(GateDecision.deny)

    add("gate_checks_passed", "all gate checks passed")
    return finish(GateDecision.allow)
