"""Policy: loads trust_policy.yaml into a typed TrustPolicy, and the pure
engine functions that read it. No DB access anywhere in this module — callers
(core.py) fetch beliefs/sources from the store and pass them in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import yaml

from .models import (
    Belief,
    ConflictResolution,
    EpistemicStatus,
    GateDecision,
    GateResult,
    RiskTier,
    TrustPolicy,
)


def load_policy(path: str) -> TrustPolicy:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return TrustPolicy.model_validate(raw)


def clamp_status(
    proposed: EpistemicStatus, source_type: str, policy: TrustPolicy
) -> EpistemicStatus:
    """The engine, not the LLM, decides the committed status (spec: "the store is
    authoritative on status"). A source may never self-assert a status stronger
    than its configured ceiling in trust_policy.yaml."""
    if source_type not in policy.source_status_ceiling:
        raise ValueError(f"unknown source type (not in trust_policy.yaml): {source_type!r}")
    ceiling = policy.source_status_ceiling[source_type]
    if policy.status_strength[proposed.value] > policy.status_strength[ceiling.value]:
        return ceiling
    return proposed


def _rank(source_type: str, ranking: list[str]) -> int:
    """Position in the trust matrix's ranking (lower = more authoritative).
    A source type absent from the ranking is treated as least authoritative."""
    return ranking.index(source_type) if source_type in ranking else len(ranking)


def _recency(belief: Belief) -> float:
    return datetime.fromisoformat(belief.valid_from).timestamp()


def resolve_conflict(
    beliefs: list[Belief],
    decision_type: Optional[str],
    policy: TrustPolicy,
    source_types: dict[str, str],
) -> ConflictResolution:
    """Pick a winner among beliefs that share one structural key. Per D2
    (PLAN.md §0), this does not delete or mute the losers — they stay current
    in the store; this only decides who a *decision* defers to right now."""
    if not beliefs:
        raise ValueError("resolve_conflict requires at least one belief")

    rule = policy.trust_matrix.get(decision_type) if decision_type else None
    if rule is not None:
        rule_id = rule.rule_id
        ranking = rule.ranking

        def sort_key(b: Belief):
            return (_rank(source_types[b.source_id], ranking), -policy.status_strength[b.status.value], -_recency(b))
    else:
        rule_id = "fallback:strength+recency"

        def sort_key(b: Belief):
            return (-policy.status_strength[b.status.value], -_recency(b))

    ordered = sorted(beliefs, key=sort_key)
    winner, losers = ordered[0], ordered[1:]
    contradicted = any(b.value != winner.value for b in losers)
    return ConflictResolution(winner=winner, losers=losers, rule_id=rule_id, contradicted=contradicted)


def gate(
    action: str,
    supporting_beliefs: list[Belief],
    policy: TrustPolicy,
    source_types: dict[str, str],
    *,
    agent_id: Optional[str] = None,
) -> GateResult:
    """The action gate. Retrieval never implies permission (spec principle 4) —
    this is the only path that may authorize an action, and it never mutates
    anything; it only decides allow | deny | needs_human with a reason chain."""
    if action not in policy.actions:
        raise ValueError(f"unknown action: {action!r}")
    action_spec = policy.actions[action]
    decision_type = action_spec.decision
    rule = policy.gate_rules[action_spec.risk]
    reasons = [f"action '{action}' is tier '{action_spec.risk.value}' (decision_type={decision_type})"]

    if agent_id is not None:
        agent_perm = policy.agents.get(agent_id)
        if agent_perm is None:
            return GateResult(decision=GateDecision.deny, reasons=[*reasons, f"unknown agent_id: {agent_id!r}"])
        tiers = list(RiskTier)
        if tiers.index(action_spec.risk) > tiers.index(agent_perm.max_tier):
            return GateResult(decision=GateDecision.deny, reasons=[
                *reasons,
                f"agent '{agent_id}' is limited to '{agent_perm.max_tier.value}' tier; "
                f"'{action}' requires '{action_spec.risk.value}'",
            ])

    if not supporting_beliefs:
        return GateResult(decision=GateDecision.deny, reasons=[*reasons, "no supporting beliefs for this decision"])

    conflict = resolve_conflict(supporting_beliefs, decision_type, policy, source_types)
    winner = conflict.winner
    reasons.append(
        f"winning belief: {winner.entity}.{winner.attribute}={winner.value!r} "
        f"[{winner.status.value} · source={winner.source_id}] (rule {conflict.rule_id})"
    )
    if conflict.contradicted:
        disagreeing = [b for b in conflict.losers if b.value != winner.value]
        reasons.append(
            "conflicting belief(s): "
            + ", ".join(f"{b.value!r} [{b.status.value} · source={b.source_id}]" for b in disagreeing)
        )

    if winner.status == EpistemicStatus.disputed and action_spec.risk != RiskTier.informational:
        return GateResult(decision=GateDecision.needs_human, reasons=[
            *reasons, "winning belief is disputed; escalating to human review rather than auto-deciding",
        ])

    if rule.require_current and policy.status_strength[winner.status.value] == 0:
        return GateResult(decision=GateDecision.deny, reasons=[
            *reasons, f"winning belief status '{winner.status.value}' is no longer usable",
        ])

    if policy.status_strength[winner.status.value] < policy.status_strength[rule.min_status.value]:
        return GateResult(decision=GateDecision.deny, reasons=[
            *reasons,
            f"status '{winner.status.value}' is below the '{rule.min_status.value}' floor "
            f"required for '{action_spec.risk.value}' actions",
        ])

    trust_rule = policy.trust_matrix.get(decision_type)

    if rule.require_authoritative_source:
        authoritative = trust_rule.authoritative if trust_rule else []
        if source_types[winner.source_id] not in authoritative:
            return GateResult(decision=GateDecision.deny, reasons=[
                *reasons,
                f"source '{source_types[winner.source_id]}' is not authoritative for "
                f"'{decision_type}' (authoritative: {authoritative})",
            ])

    if rule.require_uncontradicted:
        ranking = trust_rule.ranking if trust_rule else []
        winner_rank = _rank(source_types[winner.source_id], ranking)
        equal_or_higher_disagrees = any(
            _rank(source_types[b.source_id], ranking) <= winner_rank and b.value != winner.value
            for b in conflict.losers
        )
        if equal_or_higher_disagrees:
            return GateResult(decision=GateDecision.deny, reasons=[
                *reasons, "an equally-or-more-authoritative source disagrees with the winning belief",
            ])

    if action_spec.require_value is not None and winner.value != action_spec.require_value:
        return GateResult(decision=GateDecision.deny, reasons=[
            *reasons,
            f"required value '{action_spec.require_value}' but the winning belief asserts "
            f"'{winner.value}'",
        ])

    return GateResult(decision=GateDecision.allow, reasons=[*reasons, "all gate checks passed"])
