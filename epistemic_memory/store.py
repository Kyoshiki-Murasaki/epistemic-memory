"""SQLite data-access layer. The ONLY module (besides schema.sql itself) that
imports sqlite3 — see PLAN.md §2b / 02_SPEC.md "foundation requirements" #1.

Beliefs and events are append-only (D1, PLAN.md §0): there is no update/edit
path here at all, only insert. "Current" and "valid_to" are derived from the
supersedes_id chain at read time, never stored then mutated.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Belief, Event, Source


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_belief(row: sqlite3.Row, *, is_current: bool) -> Belief:
    return Belief(
        id=row["id"],
        entity=row["entity"],
        attribute=row["attribute"],
        value=row["value"],
        status=row["status"],
        scope=row["scope"],
        source_id=row["source_id"],
        event_id=row["event_id"],
        supersedes_id=row["supersedes_id"],
        decision_type=row["decision_type"],
        valid_from=row["valid_from"],
        created_at=row["created_at"],
        is_current=is_current,
    )


class Store:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        schema = (Path(__file__).parent / "schema.sql").read_text()
        self.conn.executescript(schema)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------- sources -------------------------------

    def add_source(self, source: Source) -> Source:
        source = source.model_copy(update={"created_at": source.created_at or _now()})
        self.conn.execute(
            "INSERT INTO sources (id, type, label, created_at) VALUES (?, ?, ?, ?)",
            (source.id, source.type, source.label, source.created_at),
        )
        self.conn.commit()
        return source

    def get_source(self, source_id: str) -> Optional[Source]:
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        return Source(id=row["id"], type=row["type"], label=row["label"], created_at=row["created_at"])

    # -------------------------------- events -------------------------------

    def add_event(self, event: Event) -> Event:
        meta_json = json.dumps(event.meta) if event.meta is not None else None
        cur = self.conn.execute(
            "INSERT INTO events (source_id, content, scope, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.source_id, event.content, event.scope, meta_json, event.created_at),
        )
        self.conn.commit()
        return event.model_copy(update={"id": cur.lastrowid})

    # ------------------------------- beliefs -------------------------------

    def add_belief(self, belief: Belief) -> Belief:
        if self.get_source(belief.source_id) is None:
            raise ValueError(f"no such source: {belief.source_id!r}")
        if belief.event_id is not None:
            event = self.conn.execute(
                "SELECT source_id FROM events WHERE id = ?", (belief.event_id,)
            ).fetchone()
            if event is None:
                raise ValueError(f"no such event: {belief.event_id}")
            if event["source_id"] != belief.source_id:
                raise ValueError(
                    f"belief source {belief.source_id!r} does not match event "
                    f"source {event['source_id']!r}"
                )
        cur = self.conn.execute(
            "INSERT INTO beliefs (entity, attribute, value, status, scope, source_id, "
            "event_id, supersedes_id, decision_type, valid_from, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                belief.entity,
                belief.attribute,
                belief.value,
                belief.status.value,
                belief.scope,
                belief.source_id,
                belief.event_id,
                belief.supersedes_id,
                belief.decision_type,
                belief.valid_from,
                belief.created_at,
            ),
        )
        self.conn.commit()
        return belief.model_copy(update={"id": cur.lastrowid, "is_current": True})

    def supersede(self, old_belief_id: int, new_belief: Belief) -> Belief:
        old = self.get_belief(old_belief_id)
        if old is None:
            raise ValueError(f"no such belief: {old_belief_id}")
        if old.key != (new_belief.entity, new_belief.attribute):
            raise ValueError(
                f"supersede key mismatch: {old.key} != {(new_belief.entity, new_belief.attribute)}"
            )
        if old.source_id != new_belief.source_id:
            raise ValueError(
                "supersession is same-source only; cross-source disagreement must coexist"
            )
        if not old.is_current:
            raise ValueError(f"belief {old_belief_id} is already superseded")
        return self.add_belief(new_belief.model_copy(update={"supersedes_id": old_belief_id}))

    def get_belief(self, belief_id: int) -> Optional[Belief]:
        row = self.conn.execute(
            "SELECT b.*, NOT EXISTS "
            "(SELECT 1 FROM beliefs newer WHERE newer.supersedes_id = b.id) AS is_current "
            "FROM beliefs b WHERE b.id = ?",
            (belief_id,),
        ).fetchone()
        return _row_to_belief(row, is_current=bool(row["is_current"])) if row else None

    def is_current(self, belief_id: int) -> bool:
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM beliefs WHERE id = ?) AS exists_flag, "
            "NOT EXISTS(SELECT 1 FROM beliefs WHERE supersedes_id = ?) AS current_flag",
            (belief_id, belief_id),
        ).fetchone()
        return bool(row["exists_flag"] and row["current_flag"])

    def valid_to(self, belief_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT valid_from FROM beliefs WHERE supersedes_id = ? ORDER BY valid_from, id LIMIT 1",
            (belief_id,),
        ).fetchone()
        return row["valid_from"] if row else None

    def current_beliefs(self, entity: str, attribute: str) -> list[Belief]:
        rows = self.conn.execute(
            "SELECT * FROM beliefs WHERE entity = ? AND attribute = ? "
            "AND id NOT IN (SELECT supersedes_id FROM beliefs WHERE supersedes_id IS NOT NULL) "
            "ORDER BY valid_from, id",
            (entity, attribute),
        ).fetchall()
        return [_row_to_belief(r, is_current=True) for r in rows]

    def belief_chain(self, belief_id: int) -> list[Belief]:
        """Walk supersedes_id backward from belief_id to its root. Oldest first."""
        chain: list[Belief] = []
        current = self.get_belief(belief_id)
        if current is None:
            raise ValueError(f"no such belief: {belief_id}")
        while current is not None:
            chain.append(current)
            current = self.get_belief(current.supersedes_id) if current.supersedes_id else None
        chain.reverse()
        return chain
