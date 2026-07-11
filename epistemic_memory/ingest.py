"""Ingest: append a raw event, get candidate beliefs from an extractor, engine
validates + commits. The LLM (or a test fixture) only PROPOSES a status via
CandidateBelief; this module clamps it to the source's policy ceiling and
decides supersede-vs-conflict (D2, PLAN.md §0) before ever calling store.

Structural note (spec 2b): supersession here is same-source-lineage only — a
new claim from the SAME source_id for the same (entity, attribute) supersedes
its predecessor. A different source asserting a different value is a conflict:
both stay current: resolve_conflict (M3) picks a winner per decision, at read
time, from the trust matrix. Ingest never resolves conflicts itself.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Optional

from .models import Belief, CandidateBelief, Event, IngestResult, Source, TrustPolicy
from .policy import clamp_status
from .store import Store

Extractor = Callable[[Event, str], list[CandidateBelief]]

LIVE_MODEL = "claude-sonnet-5"
_INFER_CURRENT = object()


def materialize_candidate(
    store: Store,
    policy: TrustPolicy,
    *,
    source: Source,
    event: Event,
    candidate: CandidateBelief,
    as_of: datetime,
    valid_from: Optional[str] = None,
    expected_current_belief_id: object = _INFER_CURRENT,
) -> Belief:
    """Validate, clamp, and append one candidate through the sole belief funnel.

    ``expected_current_belief_id`` is inferred for ordinary direct ingest. An
    explicit integer or ``None`` is a proposal/correction compare-and-materialize
    assertion and therefore rejects structural drift.
    """
    if event.id is None:
        raise ValueError("candidate materialization requires a persisted source event")
    if event.source_id != source.id:
        raise ValueError("candidate source does not match immutable source event")
    status = clamp_status(candidate.proposed_status, source.type, policy)
    timestamp = as_of.isoformat()
    belief = Belief(
        entity=candidate.entity,
        attribute=candidate.attribute,
        value=candidate.value,
        status=status,
        scope=candidate.scope,
        source_id=source.id,
        event_id=event.id,
        decision_type=candidate.decision_type,
        valid_from=valid_from or timestamp,
        created_at=timestamp,
    )
    same_source = [
        current
        for current in store.current_beliefs(candidate.entity, candidate.attribute)
        if current.source_id == source.id
    ]
    if len(same_source) > 1:
        raise ValueError("structural key has multiple current same-source beliefs")
    current = same_source[0] if same_source else None
    if expected_current_belief_id is not _INFER_CURRENT:
        actual_id = current.id if current is not None else None
        if actual_id != expected_current_belief_id:
            raise ValueError("same-source current belief changed before materialization")
    if current is not None:
        return store.supersede(current.id, belief)
    return store.add_belief(belief)


def ingest_event(
    store: Store,
    policy: TrustPolicy,
    *,
    source_id: str,
    content: str,
    scope: str,
    meta: Optional[dict] = None,
    extractor: Extractor,
    as_of: datetime,
    supersede_belief_id: Optional[int] = None,
) -> IngestResult:
    source = store.get_source(source_id)
    if source is None:
        raise ValueError(f"no such source: {source_id!r} (call add_source first)")

    timestamp = as_of.isoformat()
    event = store.add_event(Event(
        source_id=source_id,
        content=content,
        scope=scope,
        meta=meta,
        created_at=timestamp,
    ))
    candidates = extractor(event, source.type)

    committed: list[Belief] = []
    for candidate in candidates:
        expected = (
            _INFER_CURRENT
            if supersede_belief_id is None
            else supersede_belief_id
        )
        committed.append(materialize_candidate(
            store,
            policy,
            source=source,
            event=event,
            candidate=candidate,
            as_of=as_of,
            valid_from=timestamp,
            expected_current_belief_id=expected,
        ))

    return IngestResult(event=event, beliefs=committed)


def live_extractor(event: Event, source_type: str) -> list[CandidateBelief]:
    """Behind the --live flag / ANTHROPIC_API_KEY. Never called by tests."""
    import json

    import anthropic

    from .models import EpistemicStatus

    client = anthropic.Anthropic()
    prompt = (
        "Extract candidate beliefs from this event as a JSON array (and nothing else — "
        "no prose, no markdown fences). Each item must have exactly these keys: "
        "entity, attribute, value, proposed_status, scope, decision_type (null if none). "
        "proposed_status must be one of: " + ", ".join(s.value for s in EpistemicStatus) + ". "
        "scope must be one of: global, persona, project:<id>, task_type:<t>. "
        "Propose the status conservatively — you are not the authority on what gets "
        "committed; a stronger status will be clamped by policy regardless.\n\n"
        f"Source type: {source_type}\nEvent content: {event.content}"
    )
    response = client.messages.create(
        model=LIVE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    items = json.loads(raw)
    return [CandidateBelief.model_validate(item) for item in items]


def is_live_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
