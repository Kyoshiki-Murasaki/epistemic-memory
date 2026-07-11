"""SQLite data-access layer. The ONLY module (besides schema.sql itself) that
imports sqlite3 — see PLAN.md §2b / 02_SPEC.md "foundation requirements" #1.

Beliefs and events are append-only (D1, PLAN.md §0): there is no update/edit
path here at all, only insert. "Current" and "valid_to" are derived from the
supersedes_id chain at read time, never stored then mutated.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import (
    AuditTrace,
    Artifact,
    ArtifactPropagationState,
    ArtifactRegistrationRequest,
    Belief,
    Commitment,
    CommitmentCreateRequest,
    CommitmentPrecondition,
    CommitmentState,
    Dependency,
    DependencyEndpointKind,
    Event,
    Proposal,
    ProposalState,
    Source,
)


class StoreSchemaError(RuntimeError):
    """The existing database cannot safely satisfy the requested store mode."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


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


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        kind=row["kind"],
        execution_state=row["execution_state"],
        propagation_state=row["propagation_state"],
        scope=row["scope"],
        label=row["label"],
        reference=row["reference"],
        created_by_agent_id=row["created_by_agent_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_dependency(row: sqlite3.Row) -> Dependency:
    if row["upstream_belief_id"] is not None:
        upstream_kind = DependencyEndpointKind.belief
        upstream_id = row["upstream_belief_id"]
    else:
        upstream_kind = DependencyEndpointKind.artifact
        upstream_id = row["upstream_artifact_id"]
    return Dependency(
        id=row["id"],
        upstream_kind=upstream_kind,
        upstream_id=upstream_id,
        downstream_artifact_id=row["downstream_artifact_id"],
        created_by_agent_id=row["created_by_agent_id"],
        created_at=row["created_at"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        source_id=row["source_id"],
        content=row["content"],
        scope=row["scope"],
        meta=json.loads(row["meta"]) if row["meta"] is not None else None,
        created_at=row["created_at"],
    )


def _row_to_proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        sequence=row["sequence"],
        id=row["id"],
        source_event_id=row["source_event_id"],
        source_id=row["source_id"],
        source_type=row["source_type"],
        entity=row["entity"],
        attribute=row["attribute"],
        value=row["value"],
        proposed_status=row["proposed_status"],
        effective_status=row["effective_status"],
        scope=row["scope"],
        decision_type=row["decision_type"],
        creator_agent_id=row["creator_agent_id"],
        created_at=row["created_at"],
        policy_version=row["policy_version"],
        policy_fingerprint=row["policy_fingerprint"],
        expected_current_belief_id=row["expected_current_belief_id"],
        expected_current_absent=bool(row["expected_current_absent"]),
        creation_trace_id=row["creation_trace_id"],
        state=row["state"],
        decision_actor_id=row["decision_actor_id"],
        decided_at=row["decided_at"],
        decision_trace_id=row["decision_trace_id"],
        approved_belief_id=row["approved_belief_id"],
        terminal_reason_code=row["terminal_reason_code"],
    )


def _row_to_audit_trace(row: sqlite3.Row) -> AuditTrace:
    return AuditTrace.model_validate({
        "sequence": row["sequence"],
        "trace_id": row["trace_id"],
        "session_id": row["session_id"],
        "session_mode": row["session_mode"],
        "agent_id": row["agent_id"],
        "approval_actor_id": row["approval_actor_id"],
        "active_scope": row["active_scope"],
        "task_type": row["task_type"],
        "operation": row["operation"],
        "outcome": row["outcome"],
        "result_code": row["result_code"],
        "reason_codes": json.loads(row["reason_codes"]),
        "rule_ids": json.loads(row["rule_ids"]),
        "policy": {
            "version": row["policy_version"],
            "fingerprint": row["policy_fingerprint"],
        },
        "payload": json.loads(row["payload"]),
        "persisted": bool(row["persisted"]),
        "created_at": row["created_at"],
    })


class Store:
    def __init__(self, db_path: str, *, read_only: bool = False):
        self.read_only = read_only
        if read_only:
            path = Path(db_path).expanduser()
            if db_path == ":memory:" or not path.is_file():
                raise StoreSchemaError(
                    "ephemeral mode requires an existing file-backed database"
                )
            uri = f"{path.resolve().as_uri()}?mode=ro"
            try:
                self.conn = sqlite3.connect(uri, uri=True)
            except sqlite3.Error as exc:
                raise StoreSchemaError(
                    "ephemeral database could not be opened read-only"
                ) from exc
        else:
            self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        try:
            if read_only:
                self.conn.execute("PRAGMA query_only = ON")
                self._validate_required_schema()
            else:
                drops = [
                    *self._prepare_m6_tables(),
                    *self._prepare_m7_tables(),
                ]
                had_fts = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'beliefs_fts'"
                ).fetchone() is not None
                schema = (Path(__file__).parent / "schema.sql").read_text()
                upgrade_script = "BEGIN IMMEDIATE;\n"
                upgrade_script += "\n".join(
                    f"DROP TABLE {table};" for table in drops
                )
                upgrade_script += "\n" + schema
                if not had_fts:
                    upgrade_script += (
                        "\nINSERT INTO beliefs_fts(beliefs_fts) VALUES ('rebuild');"
                    )
                upgrade_script += "\nCOMMIT;"
                try:
                    self.conn.executescript(upgrade_script)
                except BaseException:
                    if self.conn.in_transaction:
                        self.conn.rollback()
                    raise
                self._validate_required_schema()
        except BaseException as exc:
            self.conn.close()
            if read_only and isinstance(exc, sqlite3.Error):
                raise StoreSchemaError(
                    "ephemeral database is invalid or unreadable"
                ) from exc
            raise
        self._transaction_depth = 0

    def _table_columns(self, table: str) -> set[str]:
        return {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _prepare_m7_tables(self) -> list[str]:
        """Replace only the accepted empty M0 placeholder tables.

        Both tables are preflighted before either is dropped, so unexpected or
        populated legacy data is never partially modified.
        """
        placeholders = {
            "audit_traces": {
                "id", "agent_id", "kind", "summary", "payload", "created_at"
            },
            "proposals": {"id", "candidate", "state", "created_at"},
        }
        modern = {
            "audit_traces": {
                "sequence", "trace_id", "session_id", "session_mode", "agent_id",
                "approval_actor_id", "active_scope", "task_type", "operation",
                "outcome", "result_code", "reason_codes", "rule_ids",
                "policy_version", "policy_fingerprint", "payload", "persisted",
                "created_at",
            },
            "proposals": {
                "sequence", "id", "source_event_id", "source_id", "source_type",
                "entity", "attribute", "value", "proposed_status",
                "effective_status", "scope", "decision_type", "creator_agent_id",
                "created_at", "policy_version", "policy_fingerprint",
                "expected_current_belief_id", "expected_current_absent",
                "creation_trace_id", "state", "decision_actor_id", "decided_at",
                "decision_trace_id", "approved_belief_id", "terminal_reason_code",
            },
        }
        replace: list[str] = []
        for table in ("audit_traces", "proposals"):
            columns = self._table_columns(table)
            if not columns or columns == modern[table]:
                continue
            if columns != placeholders[table]:
                raise StoreSchemaError(
                    f"unrecognized pre-M7 {table} schema cannot be upgraded safely"
                )
            count = self.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            if count:
                raise StoreSchemaError(
                    f"pre-M7 {table} rows lack required immutable audit provenance"
                )
            replace.append(table)
        return [
            table for table in ("proposals", "audit_traces") if table in replace
        ]

    def _validate_required_schema(self) -> None:
        if int(self.conn.execute("PRAGMA user_version").fetchone()[0]) != 7:
            raise StoreSchemaError("database schema version is not M7")
        if not int(self.conn.execute("PRAGMA foreign_keys").fetchone()[0]):
            raise StoreSchemaError("SQLite foreign-key enforcement is required")
        if self.read_only and not int(
            self.conn.execute("PRAGMA query_only").fetchone()[0]
        ):
            raise StoreSchemaError("ephemeral SQLite query_only enforcement is required")
        required_columns = {
            "sources": {"id", "type", "label", "created_at"},
            "events": {"id", "source_id", "content", "scope", "meta", "created_at"},
            "beliefs": {
                "id", "entity", "attribute", "value", "status", "scope",
                "source_id", "event_id", "supersedes_id", "decision_type",
                "valid_from", "created_at",
            },
            "commitments": {
                "id", "description", "owner", "beneficiary", "scope",
                "created_by_agent_id", "state", "deadline", "preconditions",
                "proof_required", "proof_reference", "created_at", "updated_at",
            },
            "artifacts": {
                "id", "kind", "execution_state", "propagation_state", "scope",
                "label", "reference", "created_by_agent_id", "created_at", "updated_at",
            },
            "dependencies": {
                "id", "upstream_belief_id", "upstream_artifact_id",
                "downstream_artifact_id", "created_by_agent_id", "created_at",
            },
            "audit_traces": {
                "sequence", "trace_id", "session_id", "session_mode", "agent_id",
                "approval_actor_id", "active_scope", "task_type", "operation",
                "outcome", "result_code", "reason_codes", "rule_ids",
                "policy_version", "policy_fingerprint", "payload", "persisted",
                "created_at",
            },
            "proposals": {
                "sequence", "id", "source_event_id", "source_id", "source_type",
                "entity", "attribute", "value", "proposed_status", "effective_status",
                "scope", "decision_type", "creator_agent_id", "created_at",
                "policy_version", "policy_fingerprint", "expected_current_belief_id",
                "expected_current_absent", "creation_trace_id", "state",
                "decision_actor_id", "decided_at", "decision_trace_id",
                "approved_belief_id", "terminal_reason_code",
            },
        }
        for table, required in required_columns.items():
            columns = self._table_columns(table)
            if columns != required:
                raise StoreSchemaError(
                    f"database lacks required M7 schema for table {table!r}"
                )
        objects = {
            (row["type"], row["name"])
            for row in self.conn.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table','trigger','index')"
            )
        }
        required_objects = {
            ("table", "beliefs_fts"),
            ("trigger", "events_no_update"),
            ("trigger", "events_no_delete"),
            ("trigger", "beliefs_no_update"),
            ("trigger", "beliefs_no_delete"),
            ("trigger", "beliefs_fts_ai"),
            ("trigger", "commitments_definition_no_update"),
            ("trigger", "commitments_no_delete"),
            ("trigger", "artifacts_definition_no_update"),
            ("trigger", "artifacts_no_delete"),
            ("trigger", "audit_traces_no_update"),
            ("trigger", "audit_traces_no_delete"),
            ("trigger", "proposals_definition_no_update"),
            ("trigger", "proposals_terminal_transition_only"),
            ("trigger", "proposals_no_delete"),
            ("index", "beliefs_key"),
            ("index", "dependencies_belief_edge"),
            ("index", "dependencies_artifact_edge"),
            ("index", "proposals_scope_state_order"),
            ("index", "proposals_approved_belief"),
        }
        missing = required_objects - objects
        if missing:
            raise StoreSchemaError(
                f"database lacks required M7 schema objects: {sorted(missing)}"
            )
        expected_indexes = {
            "beliefs_key": ("beliefs", False, ["entity", "attribute"]),
            "dependencies_belief_edge": (
                "dependencies", True, ["upstream_belief_id", "downstream_artifact_id"]
            ),
            "dependencies_artifact_edge": (
                "dependencies", True, ["upstream_artifact_id", "downstream_artifact_id"]
            ),
            "proposals_scope_state_order": (
                "proposals", False, ["scope", "state", "created_at", "sequence"]
            ),
            "proposals_approved_belief": (
                "proposals", True, ["approved_belief_id"]
            ),
        }
        for name, (table, unique, columns) in expected_indexes.items():
            index_rows = {
                row["name"]: bool(row["unique"])
                for row in self.conn.execute(f"PRAGMA index_list({table})")
            }
            actual_columns = [
                row["name"]
                for row in self.conn.execute(f"PRAGMA index_info({name})")
            ]
            if index_rows.get(name) != unique or actual_columns != columns:
                raise StoreSchemaError(f"database index {name!r} is not the M7 definition")
        fts_sql_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'beliefs_fts'"
        ).fetchone()
        if (
            fts_sql_row is None
            or "VIRTUAL TABLE" not in fts_sql_row["sql"].upper()
            or "FTS5" not in fts_sql_row["sql"].upper()
        ):
            raise StoreSchemaError("database lacks the required FTS5 belief index")

        def normalized(sql: str) -> str:
            return "".join(sql.lower().split()).replace("'", "").replace('"', "")

        trigger_fragments = {
            "events_no_update": ("beforeupdateonevents", "eventsareappend-only"),
            "events_no_delete": ("beforedeleteonevents", "eventsareappend-only"),
            "beliefs_no_update": ("beforeupdateonbeliefs", "beliefsareversioned"),
            "beliefs_no_delete": ("beforedeleteonbeliefs", "beliefsareneverdeleted"),
            "beliefs_fts_ai": ("afterinsertonbeliefs", "insertintobeliefs_fts"),
            "commitments_definition_no_update": (
                "beforeupdateoncommitments", "commitmentdefinitionisimmutable"
            ),
            "commitments_no_delete": (
                "beforedeleteoncommitments", "commitmentsarecancelled"
            ),
            "artifacts_definition_no_update": (
                "beforeupdateonartifacts", "artifactdefinitionisimmutable"
            ),
            "artifacts_no_delete": (
                "beforedeleteonartifacts", "artifactsareneverdeleted"
            ),
            "audit_traces_no_update": (
                "beforeupdateonaudit_traces", "audittracesareimmutable"
            ),
            "audit_traces_no_delete": (
                "beforedeleteonaudit_traces", "audittracesareimmutable"
            ),
            "proposals_definition_no_update": (
                "beforeupdateonproposals", "proposaldefinitionisimmutable"
            ),
            "proposals_terminal_transition_only": (
                "beforeupdateonproposals", "proposaldecisionsareterminal"
            ),
            "proposals_no_delete": (
                "beforedeleteonproposals", "proposalsareneverdeleted"
            ),
        }
        rows = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        trigger_sql = {row["name"]: normalized(row["sql"] or "") for row in rows}
        for name, fragments in trigger_fragments.items():
            sql = trigger_sql.get(name, "")
            if not all(fragment in sql for fragment in fragments):
                raise StoreSchemaError(f"database trigger {name!r} is not the M7 definition")

        table_sql = {
            row["name"]: normalized(row["sql"] or "")
            for row in self.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('audit_traces', 'proposals')"
            )
        }
        audit_sql = table_sql.get("audit_traces", "")
        if (
            "session_modein(direct,propose)" not in audit_sql
            or "check(persisted=1)" not in audit_sql
        ):
            raise StoreSchemaError("audit trace table lacks required M7 constraints")
        proposal_sql = table_sql.get("proposals", "")
        for fragment in (
            "referencesaudit_traces(trace_id)deferrableinitiallydeferred",
            "expected_current_absentin(0,1)",
            "statein(pending,approved,rejected,stale)",
        ):
            if fragment not in proposal_sql:
                raise StoreSchemaError("proposal table lacks required M7 constraints")

    def _prepare_m6_tables(self) -> list[str]:
        """Upgrade the unused M0-M5 artifact placeholders without guessing.

        The accepted M5 schema created these tables before any public API could
        populate them. Empty placeholders can therefore be replaced safely. If
        out-of-band rows exist, fail closed rather than inventing their missing
        scope, execution history, or provenance.
        """
        artifact_columns = self._table_columns("artifacts")
        dependency_columns = self._table_columns("dependencies")
        modern_artifacts = {
            "id", "kind", "execution_state", "propagation_state", "scope",
            "label", "reference", "created_by_agent_id", "created_at", "updated_at",
        }
        modern_dependencies = {
            "id", "upstream_belief_id", "upstream_artifact_id",
            "downstream_artifact_id", "created_by_agent_id", "created_at",
        }
        legacy_artifacts = {"id", "kind", "ref", "state", "created_at"}
        legacy_dependencies = {"id", "artifact_id", "belief_id"}
        if not artifact_columns and not dependency_columns:
            return []
        if (
            artifact_columns == modern_artifacts
            and dependency_columns == modern_dependencies
        ):
            return []
        if artifact_columns != legacy_artifacts or dependency_columns not in (
            set(), legacy_dependencies
        ):
            raise StoreSchemaError(
                "unrecognized pre-M6 artifact schema cannot be upgraded safely"
            )
        artifact_count = self.conn.execute(
            "SELECT COUNT(*) FROM artifacts"
        ).fetchone()[0]
        dependency_count = (
            self.conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0]
            if dependency_columns
            else 0
        )
        if artifact_count or dependency_count:
            raise RuntimeError(
                "pre-M6 artifact rows lack required scope and execution provenance"
            )
        return [*(['dependencies'] if dependency_columns else []), "artifacts"]

    def close(self) -> None:
        self.conn.close()

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        """One local SQLite transaction for an atomic public operation."""
        if self._transaction_depth != 0:
            raise RuntimeError("nested store transactions are not supported")
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._transaction_depth = 1
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._transaction_depth = 0

    # ------------------------------- sources -------------------------------

    def add_source(self, source: Source) -> Source:
        self.conn.execute(
            "INSERT INTO sources (id, type, label, created_at) VALUES (?, ?, ?, ?)",
            (source.id, source.type, source.label, source.created_at),
        )
        self._commit()
        return source

    def get_source(self, source_id: str) -> Optional[Source]:
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        return Source(id=row["id"], type=row["type"], label=row["label"], created_at=row["created_at"])

    # -------------------------------- events -------------------------------

    def add_event(self, event: Event) -> Event:
        meta_json = _canonical_json(event.meta) if event.meta is not None else None
        cur = self.conn.execute(
            "INSERT INTO events (source_id, content, scope, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.source_id, event.content, event.scope, meta_json, event.created_at),
        )
        self._commit()
        return event.model_copy(update={"id": cur.lastrowid})

    def get_event(self, event_id: int) -> Optional[Event]:
        row = self.conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return _row_to_event(row) if row is not None else None

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
        self._commit()
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

    def belief_successors(self, belief_id: int) -> list[Belief]:
        rows = self.conn.execute(
            "SELECT b.*, NOT EXISTS "
            "(SELECT 1 FROM beliefs newer WHERE newer.supersedes_id = b.id) "
            "AS is_current FROM beliefs b WHERE b.supersedes_id = ? ORDER BY b.id",
            (belief_id,),
        ).fetchall()
        return [
            _row_to_belief(row, is_current=bool(row["is_current"])) for row in rows
        ]

    # ------------------------------ audit --------------------------------

    def add_audit_trace(self, trace: AuditTrace) -> AuditTrace:
        if not trace.persisted:
            raise ValueError("only persisted audit traces may be inserted")
        if trace.session_mode.value == "ephemeral":
            raise ValueError("ephemeral audit traces cannot be persisted")
        cur = self.conn.execute(
            "INSERT INTO audit_traces (trace_id, session_id, session_mode, agent_id, "
            "approval_actor_id, active_scope, task_type, operation, outcome, "
            "result_code, reason_codes, rule_ids, policy_version, policy_fingerprint, "
            "payload, persisted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?)",
            (
                trace.trace_id,
                trace.session_id,
                trace.session_mode.value,
                trace.agent_id,
                trace.approval_actor_id,
                trace.active_scope,
                trace.task_type,
                trace.operation.value,
                trace.outcome.value,
                trace.result_code,
                _canonical_json(trace.reason_codes),
                _canonical_json(trace.rule_ids),
                trace.policy.version,
                trace.policy.fingerprint,
                _canonical_json(trace.payload.model_dump(mode="json")),
                1,
                trace.created_at.isoformat(),
            ),
        )
        self._commit()
        return trace.model_copy(update={"sequence": int(cur.lastrowid)})

    def get_audit_trace(self, trace_id: str) -> Optional[AuditTrace]:
        row = self.conn.execute(
            "SELECT * FROM audit_traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return _row_to_audit_trace(row) if row is not None else None

    def list_audit_traces(self) -> list[AuditTrace]:
        rows = self.conn.execute(
            "SELECT * FROM audit_traces ORDER BY sequence"
        ).fetchall()
        return [_row_to_audit_trace(row) for row in rows]

    # ---------------------------- proposals ------------------------------

    def add_proposal(self, proposal: Proposal) -> Proposal:
        cur = self.conn.execute(
            "INSERT INTO proposals (id, source_event_id, source_id, source_type, "
            "entity, attribute, value, proposed_status, effective_status, scope, "
            "decision_type, creator_agent_id, created_at, policy_version, "
            "policy_fingerprint, expected_current_belief_id, expected_current_absent, "
            "creation_trace_id, state, decision_actor_id, decided_at, decision_trace_id, "
            "approved_belief_id, terminal_reason_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                proposal.id,
                proposal.source_event_id,
                proposal.source_id,
                proposal.source_type,
                proposal.entity,
                proposal.attribute,
                proposal.value,
                proposal.proposed_status.value,
                proposal.effective_status.value,
                proposal.scope,
                proposal.decision_type,
                proposal.creator_agent_id,
                proposal.created_at.isoformat(),
                proposal.policy_version,
                proposal.policy_fingerprint,
                proposal.expected_current_belief_id,
                int(proposal.expected_current_absent),
                proposal.creation_trace_id,
                proposal.state.value,
                proposal.decision_actor_id,
                proposal.decided_at.isoformat() if proposal.decided_at else None,
                proposal.decision_trace_id,
                proposal.approved_belief_id,
                proposal.terminal_reason_code,
            ),
        )
        self._commit()
        return proposal.model_copy(update={"sequence": int(cur.lastrowid)})

    def get_proposal(
        self, proposal_id: str, *, scopes: Optional[list[str]] = None
    ) -> Optional[Proposal]:
        conditions = ["id = ?"]
        params: list[object] = [proposal_id]
        if scopes is not None:
            if not scopes:
                return None
            placeholders = ", ".join("?" for _ in scopes)
            conditions.append(f"scope IN ({placeholders})")
            params.extend(scopes)
        row = self.conn.execute(
            f"SELECT * FROM proposals WHERE {' AND '.join(conditions)}", params
        ).fetchone()
        return _row_to_proposal(row) if row is not None else None

    def list_proposals(
        self,
        *,
        scopes: Optional[list[str]] = None,
        states: Optional[list[ProposalState]] = None,
    ) -> list[Proposal]:
        conditions: list[str] = []
        params: list[object] = []
        if scopes is not None:
            if not scopes:
                conditions.append("0")
            else:
                placeholders = ", ".join("?" for _ in scopes)
                conditions.append(f"scope IN ({placeholders})")
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
            f"SELECT * FROM proposals WHERE {where} ORDER BY created_at, sequence",
            params,
        ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    def count_proposals(
        self, *, scopes: Optional[list[str]], invert_scopes: bool = False
    ) -> int:
        if scopes is None:
            row = self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()
            return int(row[0])
        if not scopes:
            if invert_scopes:
                row = self.conn.execute("SELECT COUNT(*) FROM proposals").fetchone()
                return int(row[0])
            return 0
        placeholders = ", ".join("?" for _ in scopes)
        operator = "NOT IN" if invert_scopes else "IN"
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM proposals WHERE scope {operator} ({placeholders})",
            scopes,
        ).fetchone()
        return int(row[0])

    def decide_proposal(
        self,
        proposal_id: str,
        *,
        state: ProposalState,
        actor_id: str,
        decided_at: datetime,
        decision_trace_id: str,
        approved_belief_id: Optional[int],
        terminal_reason_code: str,
    ) -> Proposal:
        if state == ProposalState.pending:
            raise ValueError("proposal decision must be terminal")
        cur = self.conn.execute(
            "UPDATE proposals SET state = ?, decision_actor_id = ?, decided_at = ?, "
            "decision_trace_id = ?, approved_belief_id = ?, terminal_reason_code = ? "
            "WHERE id = ? AND state = 'pending'",
            (
                state.value,
                actor_id,
                decided_at.isoformat(),
                decision_trace_id,
                approved_belief_id,
                terminal_reason_code,
                proposal_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("proposal state changed during decision")
        self._commit()
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise RuntimeError("decided proposal could not be reloaded")
        return proposal

    # ----------------------- artifacts + dependencies ---------------------

    def add_artifact(
        self,
        request: ArtifactRegistrationRequest,
        *,
        created_by_agent_id: str,
        created_at: datetime,
    ) -> Artifact:
        if request.scope is None:
            raise ValueError("artifact scope is required")
        timestamp = created_at.isoformat()
        cur = self.conn.execute(
            "INSERT INTO artifacts (kind, execution_state, propagation_state, scope, "
            "label, reference, created_by_agent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.kind.value,
                request.execution_state.value,
                ArtifactPropagationState.current.value,
                request.scope,
                request.label,
                request.reference,
                created_by_agent_id,
                timestamp,
                timestamp,
            ),
        )
        self._commit()
        artifact = self.get_artifact(int(cur.lastrowid))
        if artifact is None:
            raise RuntimeError("inserted artifact could not be reloaded")
        return artifact

    def get_artifact(self, artifact_id: int) -> Optional[Artifact]:
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return _row_to_artifact(row) if row is not None else None

    def list_artifacts(self) -> list[Artifact]:
        rows = self.conn.execute("SELECT * FROM artifacts ORDER BY id").fetchall()
        return [_row_to_artifact(row) for row in rows]

    def set_artifact_propagation_state(
        self,
        artifact_id: int,
        *,
        state: ArtifactPropagationState,
        updated_at: datetime,
    ) -> tuple[Artifact, ArtifactPropagationState, bool]:
        current = self.get_artifact(artifact_id)
        if current is None:
            raise ValueError(f"no such artifact: {artifact_id}")
        if updated_at < current.updated_at:
            raise ValueError("artifact timestamp must not move backward")
        previous = current.propagation_state
        changed = previous != state
        if changed:
            self.conn.execute(
                "UPDATE artifacts SET propagation_state = ?, updated_at = ? WHERE id = ?",
                (state.value, updated_at.isoformat(), artifact_id),
            )
            self._commit()
        stored = self.get_artifact(artifact_id)
        if stored is None:
            raise RuntimeError("updated artifact could not be reloaded")
        return stored, previous, changed

    def get_dependency(
        self,
        upstream_kind: DependencyEndpointKind,
        upstream_id: int,
        downstream_artifact_id: int,
    ) -> Optional[Dependency]:
        column = (
            "upstream_belief_id"
            if upstream_kind == DependencyEndpointKind.belief
            else "upstream_artifact_id"
        )
        row = self.conn.execute(
            f"SELECT * FROM dependencies WHERE {column} = ? "
            "AND downstream_artifact_id = ?",
            (upstream_id, downstream_artifact_id),
        ).fetchone()
        return _row_to_dependency(row) if row is not None else None

    def add_dependency(
        self,
        upstream_kind: DependencyEndpointKind,
        upstream_id: int,
        downstream_artifact_id: int,
        *,
        created_by_agent_id: str,
        created_at: datetime,
    ) -> Dependency:
        upstream_belief_id = (
            upstream_id if upstream_kind == DependencyEndpointKind.belief else None
        )
        upstream_artifact_id = (
            upstream_id if upstream_kind == DependencyEndpointKind.artifact else None
        )
        cur = self.conn.execute(
            "INSERT INTO dependencies (upstream_belief_id, upstream_artifact_id, "
            "downstream_artifact_id, created_by_agent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                upstream_belief_id,
                upstream_artifact_id,
                downstream_artifact_id,
                created_by_agent_id,
                created_at.isoformat(),
            ),
        )
        self._commit()
        row = self.conn.execute(
            "SELECT * FROM dependencies WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        if row is None:
            raise RuntimeError("inserted dependency could not be reloaded")
        return _row_to_dependency(row)

    def downstream_artifact_ids(
        self, upstream_kind: DependencyEndpointKind, upstream_id: int
    ) -> list[int]:
        column = (
            "upstream_belief_id"
            if upstream_kind == DependencyEndpointKind.belief
            else "upstream_artifact_id"
        )
        rows = self.conn.execute(
            f"SELECT downstream_artifact_id FROM dependencies WHERE {column} = ? "
            "ORDER BY downstream_artifact_id",
            (upstream_id,),
        ).fetchall()
        return [int(row["downstream_artifact_id"]) for row in rows]

    def artifact_adjacency(self) -> dict[int, list[int]]:
        rows = self.conn.execute(
            "SELECT upstream_artifact_id, downstream_artifact_id FROM dependencies "
            "WHERE upstream_artifact_id IS NOT NULL "
            "ORDER BY upstream_artifact_id, downstream_artifact_id"
        ).fetchall()
        adjacency: dict[int, list[int]] = {}
        for row in rows:
            adjacency.setdefault(int(row["upstream_artifact_id"]), []).append(
                int(row["downstream_artifact_id"])
            )
        return adjacency

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
        self._commit()
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
            raise RuntimeError("commitment state changed during transition")
        self._commit()
        stored = self.get_commitment(commitment_id)
        if stored is None:
            raise RuntimeError("transitioned commitment could not be reloaded")
        return stored
