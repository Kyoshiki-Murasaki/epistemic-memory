"""Epistemic context rendering, permission derivation, budgeting, and receipt."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass

from .models import (
    AssembledContext,
    AssemblyRequest,
    ConflictGroup,
    DedupDecision,
    EpistemicStatus,
    ExclusionSummary,
    MemoryReceipt,
    PermissionEntry,
    ReceiptBelief,
    RetrievalRequest,
    RetrievedBelief,
    TokenMeter,
    TrustPolicy,
)
from .policy import PolicyEvaluationError, gate, resolve_conflict
from .retrieve import retrieve_beliefs
from .store import Store

TOKEN_METHOD = "regex_words_and_punctuation_v1"
TOKEN_RULE = "R-TOKEN-001"
BUDGET_RULE = "R-BUDGET-001"
_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def estimate_tokens(text: str) -> int:
    """Deterministic pilot estimate: one token per word or punctuation mark."""
    return len(_TOKEN_RE.findall(text))


def _safe(value: object) -> str:
    """Render one dynamic field without allowing text-boundary injection."""
    return json.dumps(str(value), ensure_ascii=True)


def _trust_interpretation(status: EpistemicStatus) -> str:
    if status == EpistemicStatus.system_verified:
        return "verified"
    if status == EpistemicStatus.corroborated:
        return "corroborated"
    if status == EpistemicStatus.ai_inferred:
        return "inferred"
    if status == EpistemicStatus.disputed:
        return "disputed"
    if status in {
        EpistemicStatus.considering,
        EpistemicStatus.planned,
        EpistemicStatus.promised,
    }:
        return "intent"
    return "unverified"


def _build_conflicts(
    items: list[RetrievedBelief], policy: TrustPolicy
) -> list[ConflictGroup]:
    by_key: dict[tuple[str, str], list[RetrievedBelief]] = defaultdict(list)
    for item in items:
        by_key[item.belief.key].append(item)

    conflicts: list[ConflictGroup] = []
    for (entity, attribute), group in sorted(by_key.items()):
        # Exact stored values define structural disagreement, matching M3's
        # conflict resolver. Same-source formatting restatements are removed by
        # retrieval dedup before this point.
        if len({item.belief.value for item in group}) < 2:
            continue
        source_types = {item.source.id: item.source.type for item in group}
        try:
            resolution = resolve_conflict(
                [item.belief for item in group], attribute, policy, source_types
            )
            winner_id = resolution.winner.id
            rule_id = resolution.rule_id
            reason_code = resolution.reason_code
        except PolicyEvaluationError as exc:
            winner_id = None
            rule_id = "R-CONFLICT-UNCONFIGURED-001"
            reason_code = exc.code
        conflicts.append(ConflictGroup(
            group_id=f"conflict:{entity}:{attribute}",
            entity=entity,
            attribute=attribute,
            belief_ids=[item.belief.id for item in sorted(group, key=lambda item: item.rank)],
            winner_id=winner_id,
            rule_id=rule_id,
            reason_code=reason_code,
        ))
    return conflicts


def _render_belief(item: RetrievedBelief, policy: TrustPolicy) -> str:
    belief = item.belief
    strength = policy.status_strength[belief.status.value]
    return (
        f"[belief:{belief.id} | status:{_safe(belief.status.value)} | "
        f"trust:{_safe(_trust_interpretation(belief.status))} | strength:{strength} | "
        f"at:{_safe(belief.valid_from)} | "
        f"source:{_safe(item.source.id)}/{_safe(item.source.type)} | "
        f"scope:{_safe(belief.scope)}] "
        f"{_safe(belief.entity)}.{_safe(belief.attribute)} = {_safe(belief.value)}"
    )


@dataclass(frozen=True)
class _AtomicBlock:
    items: tuple[RetrievedBelief, ...]
    conflict: ConflictGroup | None


def _build_blocks(
    items: list[RetrievedBelief], conflicts: list[ConflictGroup]
) -> list[_AtomicBlock]:
    conflict_by_belief = {
        belief_id: conflict
        for conflict in conflicts
        for belief_id in conflict.belief_ids
    }
    item_by_id = {item.belief.id: item for item in items}
    processed: set[int] = set()
    blocks: list[_AtomicBlock] = []
    for item in sorted(items, key=lambda candidate: candidate.rank):
        belief_id = item.belief.id
        if belief_id in processed:
            continue
        conflict = conflict_by_belief.get(belief_id)
        if conflict is None:
            block_items = (item,)
        else:
            block_items = tuple(
                sorted(
                    (item_by_id[candidate_id] for candidate_id in conflict.belief_ids),
                    key=lambda candidate: candidate.rank,
                )
            )
        processed.update(candidate.belief.id for candidate in block_items)
        blocks.append(_AtomicBlock(items=block_items, conflict=conflict))
    return blocks


def _render_block(block: _AtomicBlock, policy: TrustPolicy) -> str:
    lines: list[str] = []
    if block.conflict is not None:
        winner = block.conflict.winner_id if block.conflict.winner_id is not None else "none"
        lines.append(
            "⚠ CONFLICT "
            f"[group:{_safe(block.conflict.group_id)} | rule:{_safe(block.conflict.rule_id)} | "
            f"reason:{_safe(block.conflict.reason_code)} | winner:{winner} | "
            "all_sides_retained:true]"
        )
    lines.extend(_render_belief(item, policy) for item in block.items)
    return "\n".join(lines)


def _permissions(
    items: list[RetrievedBelief], policy: TrustPolicy, agent_id: str
) -> list[PermissionEntry]:
    by_key: dict[tuple[str, str], list[RetrievedBelief]] = defaultdict(list)
    for item in items:
        by_key[item.belief.key].append(item)

    permissions: list[PermissionEntry] = []
    for (entity, attribute), group in sorted(by_key.items()):
        source_types = {item.source.id: item.source.type for item in group}
        for action, spec in sorted(policy.actions.items()):
            if spec.decision != attribute:
                continue
            result = gate(
                action,
                [item.belief for item in group],
                policy,
                source_types,
                agent_id=agent_id,
            )
            permissions.append(PermissionEntry(
                entity=entity,
                attribute=attribute,
                action=action,
                decision=result.decision,
                risk_tier=spec.risk,
                decision_type=spec.decision,
                gate=result,
            ))
    return permissions


def _render_permissions(permissions: list[PermissionEntry]) -> str:
    if not permissions:
        return "- none (no configured action applies to injected evidence)"
    return "\n".join(
        f"- action:{_safe(entry.action)} | decision:{_safe(entry.decision.value)} | "
        f"risk:{_safe(entry.risk_tier.value)} | "
        f"decision_type:{_safe(entry.decision_type)} | "
        f"rules:{','.join(_safe(rule) for rule in entry.gate.rule_ids) or _safe('none')} | "
        f"codes:{','.join(_safe(code) for code in entry.gate.reason_codes)}"
        for entry in permissions
    )


def _unique(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _build_receipt(
    items: list[RetrievedBelief],
    conflicts: list[ConflictGroup],
    permissions: list[PermissionEntry],
    deduplications: list[DedupDecision],
    exclusions: list[ExclusionSummary],
    token_budget: int,
    tokens_used: int,
) -> MemoryReceipt:
    conflict_by_belief = {
        belief_id: conflict.group_id
        for conflict in conflicts
        for belief_id in conflict.belief_ids
    }
    included = [ReceiptBelief(
        belief_id=item.belief.id,
        status=item.belief.status,
        source_id=item.source.id,
        source_type=item.source.type,
        scope=item.belief.scope,
        admitted_by=item.admitted_by,
        rank=item.rank,
        rank_factors=item.rank_factors,
        conflict_group_id=conflict_by_belief.get(item.belief.id),
    ) for item in sorted(items, key=lambda candidate: candidate.rank)]
    conflict_rules = _unique(conflict.rule_id for conflict in conflicts)
    permission_rules = _unique(
        rule_id for permission in permissions for rule_id in permission.gate.rule_ids
    )
    all_rules = _unique([
        *(rule for item in included for rule in item.admitted_by),
        *(decision.rule_id for decision in deduplications),
        *(exclusion.rule_id for exclusion in exclusions),
        *conflict_rules,
        *permission_rules,
        TOKEN_RULE,
        BUDGET_RULE,
    ])
    return MemoryReceipt(
        included=included,
        exclusions=exclusions,
        deduplications=deduplications,
        conflict_rule_ids=conflict_rules,
        permission_rule_ids=permission_rules,
        rule_ids=all_rules,
        tokens=TokenMeter(
            used=tokens_used,
            budget=token_budget,
            method=TOKEN_METHOD,
            rule_id=TOKEN_RULE,
        ),
    )


def _render_receipt(
    receipt: MemoryReceipt,
    conflicts: list[ConflictGroup],
    permissions: list[PermissionEntry],
) -> str:
    lines = ["MEMORY RECEIPT", "included:"]
    if receipt.included:
        for item in receipt.included:
            factors = item.rank_factors
            lines.append(
                f"- belief:{item.belief_id} status:{_safe(item.status.value)} "
                f"source:{_safe(item.source_id)}/{_safe(item.source_type)} "
                f"scope:{_safe(item.scope)} rank:{item.rank} "
                f"factors:(task={factors.task_relevance},status={factors.status_strength},"
                f"recency={factors.recency_micros},id={factors.tie_break_id}) "
                f"admitted:{','.join(_safe(rule) for rule in item.admitted_by)}"
            )
    else:
        lines.append("- none")

    lines.append("conflicts:")
    if conflicts:
        for conflict in conflicts:
            lines.append(
                f"- group:{_safe(conflict.group_id)} "
                f"beliefs:{','.join(str(value) for value in conflict.belief_ids)} "
                f"winner:{conflict.winner_id if conflict.winner_id is not None else 'none'} "
                f"rule:{_safe(conflict.rule_id)} reason:{_safe(conflict.reason_code)}"
            )
    else:
        lines.append("- none")

    lines.append("permissions:")
    if permissions:
        for entry in permissions:
            lines.append(
                f"- action:{_safe(entry.action)} decision:{_safe(entry.decision.value)} "
                f"rules:{','.join(_safe(rule) for rule in entry.gate.rule_ids) or _safe('none')}"
            )
    else:
        lines.append("- none")

    lines.append("deduplication:")
    if receipt.deduplications:
        for decision in receipt.deduplications:
            lines.append(
                f"- kept:{decision.representative_id} "
                f"dropped:{','.join(str(value) for value in decision.dropped_ids)} "
                f"rule:{_safe(decision.rule_id)}"
            )
    else:
        lines.append("- none")

    lines.append("exclusions:")
    for exclusion in receipt.exclusions:
        lines.append(
            f"- rule:{_safe(exclusion.rule_id)} code:{_safe(exclusion.reason_code)} "
            f"count:{exclusion.count} detail:{_safe(exclusion.safe_detail)}"
        )
    lines.append(f"rules:{','.join(_safe(rule) for rule in receipt.rule_ids)}")
    lines.append(
        f"tokens:used={receipt.tokens.used} budget={receipt.tokens.budget} "
        f"method={_safe(receipt.tokens.method)} rule={_safe(receipt.tokens.rule_id)}"
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class _RenderedState:
    text: str
    receipt_text: str
    conflicts: list[ConflictGroup]
    permissions: list[PermissionEntry]
    receipt: MemoryReceipt
    tokens: int


def _render_state(
    items: list[RetrievedBelief],
    all_conflicts: list[ConflictGroup],
    policy: TrustPolicy,
    agent_id: str,
    deduplications: list[DedupDecision],
    base_exclusions: list[ExclusionSummary],
    token_budget: int,
    budget_excluded: int,
) -> _RenderedState:
    selected_ids = {item.belief.id for item in items}
    conflicts = [
        conflict
        for conflict in all_conflicts
        if set(conflict.belief_ids).issubset(selected_ids)
    ]
    blocks = _build_blocks(items, conflicts)
    block_text = "\n\n".join(_render_block(block, policy) for block in blocks)
    if not block_text:
        block_text = "- none"
    permissions = _permissions(items, policy, agent_id)
    permission_text = _render_permissions(permissions)
    exclusions = [
        *base_exclusions,
        ExclusionSummary(
            reason_code="token_budget",
            rule_id=BUDGET_RULE,
            count=budget_excluded,
            safe_detail="complete belief or conflict block did not fit",
        ),
    ]

    receipt = _build_receipt(
        items,
        conflicts,
        permissions,
        deduplications,
        exclusions,
        token_budget,
        tokens_used=0,
    )
    receipt_text = _render_receipt(receipt, conflicts, permissions)
    text = (
        f"EPISTEMIC MEMORY CONTEXT\n{block_text}\n\n"
        f"PERMISSIONS\n{permission_text}\n\n{receipt_text}"
    )
    tokens = estimate_tokens(text)
    receipt = receipt.model_copy(update={
        "tokens": receipt.tokens.model_copy(update={"used": tokens})
    })
    receipt_text = _render_receipt(receipt, conflicts, permissions)
    text = (
        f"EPISTEMIC MEMORY CONTEXT\n{block_text}\n\n"
        f"PERMISSIONS\n{permission_text}\n\n{receipt_text}"
    )
    if estimate_tokens(text) != tokens:
        raise RuntimeError("token estimate changed while rendering its numeric receipt")
    return _RenderedState(
        text=text,
        receipt_text=receipt_text,
        conflicts=conflicts,
        permissions=permissions,
        receipt=receipt,
        tokens=tokens,
    )


def assemble_context(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: AssemblyRequest,
) -> AssembledContext:
    retrieval_request = RetrievalRequest.model_validate(
        request.model_dump(exclude={"token_budget"})
    )
    retrieval = retrieve_beliefs(store, policy, agent_id, retrieval_request)
    all_conflicts = _build_conflicts(retrieval.items, policy)
    blocks = _build_blocks(retrieval.items, all_conflicts)

    empty = _render_state(
        [],
        all_conflicts,
        policy,
        agent_id,
        retrieval.deduplications,
        retrieval.exclusions,
        request.token_budget,
        budget_excluded=0,
    )
    if empty.tokens > request.token_budget:
        raise ValueError(
            f"token_budget {request.token_budget} is below the minimum rendered "
            f"context size {empty.tokens}"
        )

    selected: list[RetrievedBelief] = []
    budget_excluded = 0
    for block in blocks:
        trial_items = sorted(
            [*selected, *block.items], key=lambda item: item.rank
        )
        trial = _render_state(
            trial_items,
            all_conflicts,
            policy,
            agent_id,
            retrieval.deduplications,
            retrieval.exclusions,
            request.token_budget,
            budget_excluded=0,
        )
        if trial.tokens <= request.token_budget:
            selected = trial_items
        else:
            budget_excluded += len(block.items)

    final = _render_state(
        selected,
        all_conflicts,
        policy,
        agent_id,
        retrieval.deduplications,
        retrieval.exclusions,
        request.token_budget,
        budget_excluded=budget_excluded,
    )
    return AssembledContext(
        request=request,
        text=final.text,
        rendered_receipt=final.receipt_text,
        items=selected,
        conflicts=final.conflicts,
        permissions=final.permissions,
        receipt=final.receipt,
        tokens_injected=final.tokens,
        token_budget=request.token_budget,
    )
