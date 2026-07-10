"""Scope-safe FTS5 retrieval and deterministic ranking for M4."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import (
    DedupDecision,
    EpistemicStatus,
    ExclusionSummary,
    RankFactors,
    RetrievalRequest,
    RetrievalResult,
    RetrievedBelief,
    Scope,
    TrustPolicy,
)
from .policy import clamp_status
from .store import Store

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def query_tokens(query: str) -> list[str]:
    """Stable tokenizer used both to quote FTS input and score candidates."""
    return list(dict.fromkeys(token.casefold() for token in _TOKEN_RE.findall(query)))


def _fts_expression(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    # Tokens come from ``\w+`` and are still quoted defensively. MATCH receives
    # this as a bound parameter, never as SQL text.
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _scope_allowed(scope: str, allowed_patterns: list[str]) -> bool:
    if scope in allowed_patterns:
        return True
    parsed = Scope.parse(scope)
    return parsed.ref is not None and f"{parsed.kind}:*" in allowed_patterns


def _recency_micros(raw: str) -> int:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000)


def _rank_factors(
    belief,
    tokens: list[str],
    request: RetrievalRequest,
    status_strength: int,
) -> RankFactors:
    entity = belief.entity.casefold()
    attribute = belief.attribute.casefold()
    value = belief.value.casefold()
    entity_hits = sum(token in entity for token in tokens)
    attribute_hits = sum(token in attribute for token in tokens)
    value_hits = sum(token in value for token in tokens)
    task_scope = f"task_type:{request.task_type}" if request.task_type else None
    scope_relevance = 2 * (belief.scope == request.scope)
    scope_relevance += int(task_scope is not None and belief.scope == task_scope)
    task_relevance = (
        4 * entity_hits
        + 2 * attribute_hits
        + value_hits
        + scope_relevance
    )
    return RankFactors(
        query_entity_hits=entity_hits,
        query_attribute_hits=attribute_hits,
        query_value_hits=value_hits,
        scope_relevance=scope_relevance,
        task_relevance=task_relevance,
        status_strength=status_strength,
        recency_micros=_recency_micros(belief.valid_from),
        tie_break_id=belief.id,
    )


def _dedup_key(item: RetrievedBelief) -> tuple:
    """Only collapse same-source, same-status, same-lineage restatements."""
    belief = item.belief
    normalized_value = " ".join(belief.value.split()).casefold()
    return (
        belief.entity,
        belief.attribute,
        normalized_value,
        belief.status.value,
        belief.scope,
        belief.source_id,
        belief.decision_type,
        belief.supersedes_id,
    )


def retrieve_beliefs(
    store: Store,
    policy: TrustPolicy,
    agent_id: str,
    request: RetrievalRequest,
) -> RetrievalResult:
    """Retrieve only authorized candidates, then rank and deduplicate them.

    FTS5 performs boolean candidate selection. The corpus-independent sort is:
    ``(-task_relevance, -policy_status_strength, -recency_micros, belief_id)``.
    No SQLite rank or wall clock participates.
    """
    tokens = query_tokens(request.query)
    fts_query = _fts_expression(tokens)
    agent = policy.agents.get(agent_id)
    if agent is None:
        return RetrievalResult(
            request=request,
            agent_id=agent_id,
            authorized=False,
            effective_scopes=[],
            query_tokens=tokens,
            items=[],
            exclusions=[ExclusionSummary(
                reason_code="agent_unknown",
                rule_id="POLICY-AGENT",
                count=0,
                safe_detail="retrieval denied before candidate inspection",
            )],
            deduplications=[],
        )

    active_scopes = list(dict.fromkeys([
        "global",
        request.scope,
        *([f"task_type:{request.task_type}"] if request.task_type else []),
    ]))
    effective_scopes = [
        scope for scope in active_scopes if _scope_allowed(scope, agent.allowed_scopes)
    ]
    agent_denied_scopes = [
        scope for scope in active_scopes if scope not in effective_scopes
    ]

    all_statuses = [status.value for status in EpistemicStatus]
    dead_status_set = {
        EpistemicStatus.superseded.value,
        EpistemicStatus.retracted.value,
        EpistemicStatus.do_not_use.value,
    }
    usable_statuses = [
        status
        for status in all_statuses
        if status not in dead_status_set and policy.status_strength[status] > 0
    ]
    dead_statuses = [status for status in all_statuses if status in dead_status_set]
    floor = (
        policy.status_strength[request.status_floor.value]
        if request.status_floor is not None
        else 0
    )
    admitted_statuses = [
        status
        for status in usable_statuses
        if policy.status_strength[status] >= floor
    ]
    below_floor_statuses = [
        status
        for status in usable_statuses
        if policy.status_strength[status] < floor
    ]
    known_source_types = list(policy.source_status_ceiling)

    common = {
        "fts_query": fts_query,
        "entity": request.entity,
        "attribute": request.attribute,
    }

    def count(
        *,
        scopes: list[str],
        invert_scopes: bool = False,
        current: bool | None = True,
        statuses: list[str] | None = None,
        invert_statuses: bool = False,
        source_types: list[str] | None = None,
        invert_source_types: bool = False,
    ) -> int:
        return store.count_beliefs(
            **common,
            scopes=scopes,
            invert_scopes=invert_scopes,
            current=current,
            statuses=statuses,
            invert_statuses=invert_statuses,
            source_types=source_types,
            invert_source_types=invert_source_types,
        )

    ceiling_excluded_ids: set[int] = set()
    timestamp_excluded_ids: set[int] = set()

    def row_is_rankable(row) -> bool:
        belief, source = row
        if clamp_status(belief.status, source.type, policy) != belief.status:
            ceiling_excluded_ids.add(belief.id)
            return False
        try:
            _recency_micros(belief.valid_from)
        except (OverflowError, ValueError):
            timestamp_excluded_ids.add(belief.id)
            return False
        return True

    seed_rows = [
        row
        for row in store.search_beliefs(
            **common,
            scopes=effective_scopes,
            current=True,
            statuses=admitted_statuses,
            source_types=known_source_types,
        )
        if row_is_rankable(row)
    ]
    selected_by_id = {
        belief.id: (belief, source) for belief, source in seed_rows
    }
    seed_ids = set(selected_by_id)
    conflict_closure_ids: set[int] = set()

    # Presentation filters select keys, never isolated conflict sides. For
    # every selected key, inspect all current usable evidence inside the same
    # scope/provenance boundary, ignoring query and status floor. Expand only
    # when that complete authorized set actually disagrees.
    for entity, attribute in sorted({belief.key for belief, _ in seed_rows}):
        closure_rows = [
            row
            for row in store.search_beliefs(
                fts_query=None,
                entity=entity,
                attribute=attribute,
                scopes=effective_scopes,
                current=True,
                statuses=usable_statuses,
                source_types=known_source_types,
            )
            if row_is_rankable(row)
        ]
        if len({belief.value for belief, _ in closure_rows}) < 2:
            continue
        for belief, source in closure_rows:
            selected_by_id[belief.id] = (belief, source)
            if belief.id not in seed_ids:
                conflict_closure_ids.add(belief.id)

    rows = [selected_by_id[belief_id] for belief_id in sorted(selected_by_id)]
    included_ids = set(selected_by_id)
    below_floor_matching_ids = {
        belief.id
        for belief, source in store.search_beliefs(
            **common,
            scopes=effective_scopes,
            current=True,
            statuses=below_floor_statuses,
            source_types=known_source_types,
        )
        if row_is_rankable((belief, source))
    }
    scope_task_count = count(
        scopes=active_scopes,
        invert_scopes=True,
        statuses=admitted_statuses,
        source_types=known_source_types,
    )
    scope_agent_count = count(
        scopes=agent_denied_scopes,
        statuses=admitted_statuses,
        source_types=known_source_types,
    )
    non_current_count = count(
        scopes=effective_scopes,
        current=False,
        statuses=admitted_statuses,
        source_types=known_source_types,
    )
    unusable_count = count(
        scopes=effective_scopes,
        statuses=dead_statuses,
        source_types=known_source_types,
    )
    below_floor_count = len(below_floor_matching_ids - included_ids)
    provenance_count = count(
        scopes=effective_scopes,
        statuses=admitted_statuses,
        source_types=known_source_types,
        invert_source_types=True,
    )

    query_rule = "R-FTS-001" if tokens else "R-NOQUERY-001"
    common_admission_rules = [
        "R-SCOPE-EFFECTIVE-001",
        "R-CURRENT-001",
        "R-USABLE-001",
        "R-PROVENANCE-001",
        "R-RANK-001",
    ]

    ranked: list[RetrievedBelief] = []
    for belief, source in rows:
        if not row_is_rankable((belief, source)):
            continue
        factors = _rank_factors(
            belief, tokens, request, policy.status_strength[belief.status.value]
        )
        item_rules = [
            (
                "R-CONFLICT-CLOSURE-001"
                if belief.id in conflict_closure_ids
                else query_rule
            ),
            *common_admission_rules,
        ]
        if request.status_floor is not None and belief.id not in conflict_closure_ids:
            item_rules.insert(-1, "R-STATUS-FLOOR-001")
        ranked.append(RetrievedBelief(
            belief=belief,
            source=source,
            rank=0,
            rank_factors=factors,
            admitted_by=item_rules,
        ))

    ranked.sort(key=lambda item: (
        -item.rank_factors.task_relevance,
        -item.rank_factors.status_strength,
        -item.rank_factors.recency_micros,
        item.rank_factors.tie_break_id,
    ))

    representatives: dict[tuple, RetrievedBelief] = {}
    dropped: dict[int, list[int]] = {}
    deduplicated: list[RetrievedBelief] = []
    for item in ranked:
        key = _dedup_key(item)
        representative = representatives.get(key)
        if representative is None:
            representatives[key] = item
            deduplicated.append(item)
            continue
        dropped.setdefault(representative.belief.id, []).append(item.belief.id)

    items = [item.model_copy(update={"rank": rank}) for rank, item in enumerate(
        deduplicated, start=1
    )]
    dedup_decisions = [
        DedupDecision(
            rule_id="R-DEDUP-001",
            representative_id=representative_id,
            dropped_ids=dropped_ids,
        )
        for representative_id, dropped_ids in dropped.items()
    ]
    exclusions = [
        ExclusionSummary(
            reason_code="task_scope_mismatch",
            rule_id="R-SCOPE-TASK-001",
            count=scope_task_count,
            safe_detail="candidate outside active task scopes",
        ),
        ExclusionSummary(
            reason_code="agent_scope_denied",
            rule_id="R-SCOPE-AGENT-001",
            count=scope_agent_count,
            safe_detail="candidate outside agent readable scopes",
        ),
        ExclusionSummary(
            reason_code="non_current",
            rule_id="R-CURRENT-001",
            count=non_current_count,
            safe_detail="candidate is structurally superseded",
        ),
        ExclusionSummary(
            reason_code="unusable_status",
            rule_id="R-USABLE-001",
            count=unusable_count,
            safe_detail="candidate status is not usable",
        ),
        ExclusionSummary(
            reason_code="below_status_floor",
            rule_id="R-STATUS-FLOOR-001",
            count=below_floor_count,
            safe_detail="candidate is below the requested policy strength",
        ),
        ExclusionSummary(
            reason_code="provenance_unconfigured",
            rule_id="R-PROVENANCE-001",
            count=provenance_count,
            safe_detail="candidate source type is not configured",
        ),
        ExclusionSummary(
            reason_code="status_exceeds_source_ceiling",
            rule_id="R-STATUS-CEILING-001",
            count=len(ceiling_excluded_ids),
            safe_detail="candidate status exceeds source authority",
        ),
        ExclusionSummary(
            reason_code="invalid_timestamp",
            rule_id="R-TIMESTAMP-001",
            count=len(timestamp_excluded_ids),
            safe_detail="candidate timestamp is not rankable",
        ),
    ]
    return RetrievalResult(
        request=request,
        agent_id=agent_id,
        authorized=True,
        effective_scopes=effective_scopes,
        query_tokens=tokens,
        items=items,
        exclusions=exclusions,
        deduplications=dedup_decisions,
    )
