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
from datetime import datetime, timezone
from typing import Callable, Optional

from .models import Belief, CandidateBelief, Event, IngestResult, TrustPolicy
from .policy import clamp_status
from .store import Store

Extractor = Callable[[Event, str], list[CandidateBelief]]

LIVE_MODEL = "claude-sonnet-5"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ingest_event(
    store: Store,
    policy: TrustPolicy,
    *,
    source_id: str,
    content: str,
    scope: str,
    meta: Optional[dict] = None,
    extractor: Extractor,
) -> IngestResult:
    source = store.get_source(source_id)
    if source is None:
        raise ValueError(f"no such source: {source_id!r} (call add_source first)")

    event = store.add_event(
        Event(source_id=source_id, content=content, scope=scope, meta=meta, created_at=_now())
    )
    candidates = extractor(event, source.type)

    committed: list[Belief] = []
    for candidate in candidates:
        status = clamp_status(candidate.proposed_status, source.type, policy)
        belief = Belief(
            entity=candidate.entity,
            attribute=candidate.attribute,
            value=candidate.value,
            status=status,
            scope=candidate.scope,
            source_id=source_id,
            event_id=event.id,
            decision_type=candidate.decision_type,
            valid_from=_now(),
            created_at=_now(),
        )
        existing = store.current_beliefs(candidate.entity, candidate.attribute)
        same_source = next((b for b in existing if b.source_id == source_id), None)
        if same_source is not None:
            committed.append(store.supersede(same_source.id, belief))
        else:
            committed.append(store.add_belief(belief))

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
