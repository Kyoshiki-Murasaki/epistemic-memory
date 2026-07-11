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

from .models import (
    Belief,
    Commitment,
    CommitmentCreateRequest,
    CommitmentPrecondition,
    CommitmentState,
    Event,
    Source,
)


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


def _row_to_commitment(row: sqlite3.Row) -> Commitment:
    return Commitment(
        id=row["id"],
        description=row["description"],
        owner=row["owner"],
        beneficiary=row["beneficiary"],
        scope=row["scope"],
        created_by_agent_id=row["created_by_agent_id"],
        state=row["state"],
        deadline=row["deadline"],
        preconditions=[
            CommitmentPrecondition.model_validate(value)
            for value in json.loads(row["preconditions"])
        ],
        proof_required=bool(row["proof_required"]),
        proof_reference=row["proof_reference"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class Store:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        schema = (Path(__file__).parent / "schema.sql").read_text()
        self.conn.executescript(schema)
        # Rebuild is idempotent and backfills beliefs created by a pre-FTS
        # schema without mutating the append-only belief rows.
        self.conn.execute("INSERT INTO beliefs_fts(beliefs_fts) VALUES ('rebuild')")
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

    def _belief_filter_sql(
        self,
        *,
        fts_query: Optional[str],
        entity: Optional[str],
        attribute: Optional[str],
        scopes: Optional[list[str]],
        invert_scopes: bool,
        current: Optional[bool],
        statuses: Optional[list[str]],
        invert_statuses: bool,
        source_types: Optional[list[str]],
        invert_source_types: bool,
    ) -> tuple[str, str, list[object]]:
        """Build only code-controlled SQL; every caller value is a parameter."""
        joins = "JOIN sources s ON s.id = b.source_id"
        conditions: list[str] = []
        params: list[object] = []

        if fts_query:
            joins += " JOIN beliefs_fts ON beliefs_fts.rowid = b.id"
            conditions.append("beliefs_fts MATCH ?")
            params.append(fts_query)
        if entity is not None:
            conditions.append("b.entity = ?")
            params.append(entity)
        if attribute is not None:
            conditions.append("b.attribute = ?")
            params.append(attribute)

        def add_set_filter(
            column: str, values: Optional[list[str]], invert: bool
        ) -> None:
            if values is None:
                return
            if not values:
                if not invert:
                    conditions.append("0")
                return
            placeholders = ", ".join("?" for _ in values)
            operator = "NOT IN" if invert else "IN"
            conditions.append(f"{column} {operator} ({placeholders})")
            params.extend(values)

        add_set_filter("b.scope", scopes, invert_scopes)
        add_set_filter("b.status", statuses, invert_statuses)
        add_set_filter("s.type", source_types, invert_source_types)

        if current is not None:
            successor = (
                "EXISTS (SELECT 1 FROM beliefs newer "
                "WHERE newer.supersedes_id = b.id)"
            )
            conditions.append(f"NOT {successor}" if current else successor)

        where = " AND ".join(conditions) if conditions else "1"
        return joins, where, params

    def search_beliefs(
        self,
        *,
        fts_query: Optional[str] = None,
        entity: Optional[str] = None,
        attribute: Optional[str] = None,
        scopes: Optional[list[str]] = None,
        invert_scopes: bool = False,
        current: Optional[bool] = True,
        statuses: Optional[list[str]] = None,
        invert_statuses: bool = False,
        source_types: Optional[list[str]] = None,
        invert_source_types: bool = False,
    ) -> list[tuple[Belief, Source]]:
        """Return candidates after SQL scope/current/provenance filtering.

        FTS5 is a boolean candidate selector only. Ranking happens later over
        this already-authorized set, so forbidden documents cannot affect it.
        """
        joins, where, params = self._belief_filter_sql(
            fts_query=fts_query,
            entity=entity,
            attribute=attribute,
            scopes=scopes,
            invert_scopes=invert_scopes,
            current=current,
            statuses=statuses,
            invert_statuses=invert_statuses,
            source_types=source_types,
            invert_source_types=invert_source_types,
        )
        current_expr = (
            "NOT EXISTS (SELECT 1 FROM beliefs newer "
            "WHERE newer.supersedes_id = b.id)"
        )
        rows = self.conn.execute(
            f"SELECT b.*, {current_expr} AS derived_current, "
            "s.type AS source_type, s.label AS source_label, "
            "s.created_at AS source_created_at "
            f"FROM beliefs b {joins} WHERE {where} ORDER BY b.id",
            params,
        ).fetchall()
        return [
            (
                _row_to_belief(row, is_current=bool(row["derived_current"])),
                Source(
                    id=row["source_id"],
                    type=row["source_type"],
                    label=row["source_label"],
                    created_at=row["source_created_at"],
                ),
            )
            for row in rows
        ]

    def count_beliefs(self, **filters) -> int:
        """Count exclusions without returning their content or identifiers."""
        joins, where, params = self._belief_filter_sql(**filters)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM beliefs b {joins} WHERE {where}", params
        ).fetchone()
        return int(row["count"])

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

    # ----------------------------- commitments ----------------------------

    def add_commitment(
        self,
        request: CommitmentCreateRequest,
        *,
        created_by_agent_id: str,
        created_at: datetime,
    ) -> Commitment:
        if request.scope is None:
            raise ValueError("commitment scope is required")
        preconditions = json.dumps(
            [value.model_dump(mode="json") for value in request.preconditions],
            separators=(",", ":"),
            sort_keys=True,
        )
        created_at_text = created_at.isoformat()
        cur = self.conn.execute(
            "INSERT INTO commitments (description, owner, beneficiary, scope, "
            "created_by_agent_id, state, deadline, preconditions, proof_required, "
            "proof_reference, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.description,
                request.owner,
                request.beneficiary,
                request.scope,
                created_by_agent_id,
                CommitmentState.open.value,
                request.deadline.isoformat(),
                preconditions,
                int(request.proof_required),
                None,
                created_at_text,
                created_at_text,
            ),
        )
        self.conn.commit()
        stored = self.get_commitment(int(cur.lastrowid))
        if stored is None:
            raise RuntimeError("inserted commitment could not be reloaded")
        return stored

    def get_commitment(self, commitment_id: int) -> Optional[Commitment]:
        row = self.conn.execute(
            "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
        ).fetchone()
        return _row_to_commitment(row) if row is not None else None

    def list_commitments(
        self,
        *,
        scopes: Optional[list[str]] = None,
        invert_scopes: bool = False,
        states: Optional[list[CommitmentState]] = None,
    ) -> list[Commitment]:
        conditions: list[str] = []
        params: list[object] = []
        if scopes is not None:
            if not scopes:
                if not invert_scopes:
                    conditions.append("0")
            else:
                placeholders = ", ".join("?" for _ in scopes)
                operator = "NOT IN" if invert_scopes else "IN"
                conditions.append(f"scope {operator} ({placeholders})")
                params.extend(scopes)
        if states is not None:
            if not states:
                conditions.append("0")
            else:
                placeholders = ", ".join("?" for _ in states)
                conditions.append(f"state IN ({placeholders})")
                params.extend(state.value for state in states)
        where = " AND ".join(conditions) if conditions else "1"
        rows = self.conn.execute(
            f"SELECT * FROM commitments WHERE {where} "
            "ORDER BY deadline, created_at, id",
            params,
        ).fetchall()
        return [_row_to_commitment(row) for row in rows]

    def count_commitments(
        self, *, scopes: Optional[list[str]], invert_scopes: bool = False
    ) -> int:
        if scopes is None:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM commitments").fetchone()
            return int(row["count"])
        if not scopes:
            if invert_scopes:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS count FROM commitments"
                ).fetchone()
                return int(row["count"])
            return 0
        placeholders = ", ".join("?" for _ in scopes)
        operator = "NOT IN" if invert_scopes else "IN"
        row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM commitments "
            f"WHERE scope {operator} ({placeholders})",
            scopes,
        ).fetchone()
        return int(row["count"])

    def update_commitment_state(
        self,
        commitment_id: int,
        *,
        expected_state: CommitmentState,
        target_state: CommitmentState,
        updated_at: datetime,
        proof_reference: Optional[str],
    ) -> Commitment:
        current = self.get_commitment(commitment_id)
        if current is None:
            raise ValueError(f"no such commitment: {commitment_id}")
        if updated_at < current.updated_at:
            raise ValueError("commitment timestamp must not move backward")
        cur = self.conn.execute(
            "UPDATE commitments SET state = ?, proof_reference = ?, updated_at = ? "
            "WHERE id = ? AND state = ?",
            (
                target_state.value,
                proof_reference,
                updated_at.isoformat(),
                commitment_id,
                expected_state.value,
            ),
        )
        if cur.rowcount != 1:
            self.conn.rollback()
            raise RuntimeError("commitment state changed during transition")
        self.conn.commit()
        stored = self.get_commitment(commitment_id)
        if stored is None:
            raise RuntimeError("transitioned commitment could not be reloaded")
        return stored
