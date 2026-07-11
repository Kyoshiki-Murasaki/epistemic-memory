-- schema.sql — Epistemic Memory pilot store.
-- Two immutable layers (events, beliefs) + operational tables around them.
-- See PLAN.md §1 for design rationale (D1: beliefs are append-only, valid_to is derived).

PRAGMA foreign_keys = ON;
PRAGMA user_version = 7;

-- ============================ sources =====================================
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    label       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- ============================ events ======================================
-- Raw, immutable "what actually happened". Append-only. Never updated/deleted.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(id),
    content     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    meta        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

-- ============================ beliefs =====================================
-- Versioned interpretation. Append-only (D1): a change is a new row linked via
-- supersedes_id. valid_to / "is this current" are DERIVED, not stored.
CREATE TABLE IF NOT EXISTS beliefs (
    id            INTEGER PRIMARY KEY,
    entity        TEXT NOT NULL,
    attribute     TEXT NOT NULL,
    value         TEXT NOT NULL,
    status        TEXT NOT NULL,
    scope         TEXT NOT NULL,
    source_id     TEXT NOT NULL REFERENCES sources(id),
    event_id      INTEGER REFERENCES events(id),
    supersedes_id INTEGER REFERENCES beliefs(id),
    decision_type TEXT,
    valid_from    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS beliefs_key ON beliefs(entity, attribute);

CREATE TRIGGER IF NOT EXISTS beliefs_no_update BEFORE UPDATE ON beliefs
BEGIN
    SELECT RAISE(ABORT, 'beliefs are versioned: supersede, do not edit');
END;

CREATE TRIGGER IF NOT EXISTS beliefs_no_delete BEFORE DELETE ON beliefs
BEGIN
    SELECT RAISE(ABORT, 'beliefs are never deleted: retract via a new version');
END;

-- FTS5 index over searchable belief text (used starting M4). Beliefs are
-- insert-only, so a single AFTER INSERT sync trigger is sufficient — there is
-- no update/delete path to keep in sync with.
CREATE VIRTUAL TABLE IF NOT EXISTS beliefs_fts USING fts5(
    entity, attribute, value, content='beliefs', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS beliefs_fts_ai AFTER INSERT ON beliefs
BEGIN
    INSERT INTO beliefs_fts(rowid, entity, attribute, value)
    VALUES (new.id, new.entity, new.attribute, new.value);
END;

-- ============================ commitments =================================
-- First-class promises/obligations. Operational state (not a belief). M7 owns
-- persistent audit traces; M5 records only the immutable managing principal.
CREATE TABLE IF NOT EXISTS commitments (
    id                    INTEGER PRIMARY KEY,
    description           TEXT NOT NULL,
    owner                 TEXT NOT NULL,
    beneficiary           TEXT NOT NULL,
    scope                 TEXT NOT NULL,
    created_by_agent_id   TEXT NOT NULL,
    state                 TEXT NOT NULL CHECK (
        state IN ('open', 'waiting', 'fulfilled', 'cancelled', 'overdue')
    ),
    deadline              TEXT NOT NULL,
    preconditions         TEXT NOT NULL,
    proof_required        INTEGER NOT NULL CHECK (proof_required IN (0, 1)),
    proof_reference       TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK (proof_reference IS NULL OR state = 'fulfilled'),
    CHECK (state != 'fulfilled' OR proof_required = 0 OR proof_reference IS NOT NULL)
);

-- A transition may change only operational state, retained proof, and its
-- timestamp. In particular, creator provenance and the domain owner are
-- immutable and cannot be conflated or silently rewritten.
CREATE TRIGGER IF NOT EXISTS commitments_definition_no_update
BEFORE UPDATE ON commitments
WHEN NEW.id IS NOT OLD.id
  OR NEW.description IS NOT OLD.description
  OR NEW.owner IS NOT OLD.owner
  OR NEW.beneficiary IS NOT OLD.beneficiary
  OR NEW.scope IS NOT OLD.scope
  OR NEW.created_by_agent_id IS NOT OLD.created_by_agent_id
  OR NEW.deadline IS NOT OLD.deadline
  OR NEW.preconditions IS NOT OLD.preconditions
  OR NEW.proof_required IS NOT OLD.proof_required
  OR NEW.created_at IS NOT OLD.created_at
  OR (
      OLD.proof_reference IS NOT NULL
      AND NEW.proof_reference IS NOT OLD.proof_reference
  )
BEGIN
    SELECT RAISE(ABORT, 'commitment definition is immutable');
END;

CREATE TRIGGER IF NOT EXISTS commitments_no_delete BEFORE DELETE ON commitments
BEGIN
    SELECT RAISE(ABORT, 'commitments are cancelled, never deleted');
END;

-- ============================ artifacts + dependencies ====================
-- Derived things a correction can invalidate. What an artifact is, whether
-- an action executed, and what propagation did to it are independent facts.
CREATE TABLE IF NOT EXISTS artifacts (
    id                    INTEGER PRIMARY KEY,
    kind                  TEXT NOT NULL CHECK (kind IN ('output', 'action')),
    execution_state       TEXT NOT NULL CHECK (
        execution_state IN ('not_applicable', 'pending', 'executed')
    ),
    propagation_state     TEXT NOT NULL CHECK (
        propagation_state IN ('current', 'stale', 'halted', 'review_required')
    ),
    scope                 TEXT NOT NULL,
    label                 TEXT NOT NULL,
    reference             TEXT,
    created_by_agent_id   TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    CHECK (
        (kind = 'output' AND execution_state = 'not_applicable')
        OR (kind = 'action' AND execution_state IN ('pending', 'executed'))
    )
);

-- Propagation may change only its own state and timestamp. Stable identity,
-- provenance, execution history, scope, and safe label/reference are immutable.
CREATE TRIGGER IF NOT EXISTS artifacts_definition_no_update
BEFORE UPDATE ON artifacts
WHEN NEW.id IS NOT OLD.id
  OR NEW.kind IS NOT OLD.kind
  OR NEW.execution_state IS NOT OLD.execution_state
  OR NEW.scope IS NOT OLD.scope
  OR NEW.label IS NOT OLD.label
  OR NEW.reference IS NOT OLD.reference
  OR NEW.created_by_agent_id IS NOT OLD.created_by_agent_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'artifact definition is immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts are never deleted');
END;

-- Explicit DAG edges. Exactly one upstream foreign key is populated; the
-- downstream is always an artifact. Partial unique indexes make registration
-- deterministic for both edge kinds.
CREATE TABLE IF NOT EXISTS dependencies (
    id                       INTEGER PRIMARY KEY,
    upstream_belief_id       INTEGER REFERENCES beliefs(id),
    upstream_artifact_id     INTEGER REFERENCES artifacts(id),
    downstream_artifact_id   INTEGER NOT NULL REFERENCES artifacts(id),
    created_by_agent_id      TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    CHECK (
        (upstream_belief_id IS NOT NULL AND upstream_artifact_id IS NULL)
        OR (upstream_belief_id IS NULL AND upstream_artifact_id IS NOT NULL)
    ),
    CHECK (
        upstream_artifact_id IS NULL
        OR upstream_artifact_id != downstream_artifact_id
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS dependencies_belief_edge
ON dependencies(upstream_belief_id, downstream_artifact_id)
WHERE upstream_belief_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS dependencies_artifact_edge
ON dependencies(upstream_artifact_id, downstream_artifact_id)
WHERE upstream_artifact_id IS NOT NULL;

-- ============================ audit_traces ================================
-- Immutable M7 decision/mutation snapshots. ``sequence`` is insertion order;
-- ``trace_id`` is the stable externally returned identifier.
CREATE TABLE IF NOT EXISTS audit_traces (
    sequence            INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id            TEXT NOT NULL UNIQUE,
    session_id          TEXT NOT NULL,
    session_mode        TEXT NOT NULL CHECK (session_mode IN ('direct', 'propose')),
    agent_id            TEXT NOT NULL,
    approval_actor_id   TEXT,
    active_scope        TEXT NOT NULL,
    task_type           TEXT,
    operation           TEXT NOT NULL CHECK (operation IN (
        'ingest', 'proposal_create', 'proposal_approve', 'proposal_reject',
        'assemble', 'gate', 'commitment_create', 'commitment_transition',
        'overdue_scan', 'artifact_register', 'dependency_register', 'correction'
    )),
    outcome             TEXT NOT NULL CHECK (
        outcome IN ('completed', 'denied', 'stale')
    ),
    result_code         TEXT NOT NULL,
    reason_codes        TEXT NOT NULL,
    rule_ids            TEXT NOT NULL,
    policy_version      INTEGER NOT NULL,
    policy_fingerprint  TEXT NOT NULL,
    payload             TEXT NOT NULL,
    persisted           INTEGER NOT NULL CHECK (persisted = 1),
    created_at          TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_traces_no_update
BEFORE UPDATE ON audit_traces
BEGIN
    SELECT RAISE(ABORT, 'audit traces are immutable');
END;

CREATE TRIGGER IF NOT EXISTS audit_traces_no_delete
BEFORE DELETE ON audit_traces
BEGIN
    SELECT RAISE(ABORT, 'audit traces are immutable');
END;

-- ============================ proposals ===================================
-- Immutable candidate definition plus a one-way, audited terminal lifecycle.
CREATE TABLE IF NOT EXISTS proposals (
    sequence                    INTEGER PRIMARY KEY AUTOINCREMENT,
    id                          TEXT NOT NULL UNIQUE,
    source_event_id             INTEGER NOT NULL REFERENCES events(id),
    source_id                   TEXT NOT NULL REFERENCES sources(id),
    source_type                 TEXT NOT NULL,
    entity                      TEXT NOT NULL,
    attribute                   TEXT NOT NULL,
    value                       TEXT NOT NULL,
    proposed_status             TEXT NOT NULL,
    effective_status            TEXT NOT NULL,
    scope                       TEXT NOT NULL,
    decision_type               TEXT,
    creator_agent_id            TEXT NOT NULL,
    created_at                  TEXT NOT NULL,
    policy_version              INTEGER NOT NULL,
    policy_fingerprint          TEXT NOT NULL,
    expected_current_belief_id  INTEGER REFERENCES beliefs(id),
    expected_current_absent     INTEGER NOT NULL CHECK (
        expected_current_absent IN (0, 1)
    ),
    creation_trace_id           TEXT NOT NULL REFERENCES audit_traces(trace_id)
                                  DEFERRABLE INITIALLY DEFERRED,
    state                       TEXT NOT NULL CHECK (
        state IN ('pending', 'approved', 'rejected', 'stale')
    ),
    decision_actor_id           TEXT,
    decided_at                  TEXT,
    decision_trace_id           TEXT REFERENCES audit_traces(trace_id)
                                  DEFERRABLE INITIALLY DEFERRED,
    approved_belief_id          INTEGER REFERENCES beliefs(id),
    terminal_reason_code        TEXT,
    CHECK (
        (expected_current_absent = 1 AND expected_current_belief_id IS NULL)
        OR
        (expected_current_absent = 0 AND expected_current_belief_id IS NOT NULL)
    ),
    CHECK (
        (
            state = 'pending'
            AND decision_actor_id IS NULL
            AND decided_at IS NULL
            AND decision_trace_id IS NULL
            AND approved_belief_id IS NULL
            AND terminal_reason_code IS NULL
        )
        OR
        (
            state != 'pending'
            AND decision_actor_id IS NOT NULL
            AND decided_at IS NOT NULL
            AND decision_trace_id IS NOT NULL
            AND terminal_reason_code IS NOT NULL
            AND (
                (state = 'approved' AND approved_belief_id IS NOT NULL)
                OR (state != 'approved' AND approved_belief_id IS NULL)
            )
        )
    )
);

CREATE INDEX IF NOT EXISTS proposals_scope_state_order
ON proposals(scope, state, created_at, sequence);

CREATE UNIQUE INDEX IF NOT EXISTS proposals_approved_belief
ON proposals(approved_belief_id)
WHERE approved_belief_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS proposals_definition_no_update
BEFORE UPDATE ON proposals
WHEN NEW.sequence IS NOT OLD.sequence
  OR NEW.id IS NOT OLD.id
  OR NEW.source_event_id IS NOT OLD.source_event_id
  OR NEW.source_id IS NOT OLD.source_id
  OR NEW.source_type IS NOT OLD.source_type
  OR NEW.entity IS NOT OLD.entity
  OR NEW.attribute IS NOT OLD.attribute
  OR NEW.value IS NOT OLD.value
  OR NEW.proposed_status IS NOT OLD.proposed_status
  OR NEW.effective_status IS NOT OLD.effective_status
  OR NEW.scope IS NOT OLD.scope
  OR NEW.decision_type IS NOT OLD.decision_type
  OR NEW.creator_agent_id IS NOT OLD.creator_agent_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.policy_version IS NOT OLD.policy_version
  OR NEW.policy_fingerprint IS NOT OLD.policy_fingerprint
  OR NEW.expected_current_belief_id IS NOT OLD.expected_current_belief_id
  OR NEW.expected_current_absent IS NOT OLD.expected_current_absent
  OR NEW.creation_trace_id IS NOT OLD.creation_trace_id
BEGIN
    SELECT RAISE(ABORT, 'proposal definition is immutable');
END;

CREATE TRIGGER IF NOT EXISTS proposals_terminal_transition_only
BEFORE UPDATE ON proposals
WHEN NOT (
    OLD.state = 'pending'
    AND NEW.state IN ('approved', 'rejected', 'stale')
)
BEGIN
    SELECT RAISE(ABORT, 'proposal decisions are terminal');
END;

CREATE TRIGGER IF NOT EXISTS proposals_no_delete
BEFORE DELETE ON proposals
BEGIN
    SELECT RAISE(ABORT, 'proposals are never deleted');
END;
