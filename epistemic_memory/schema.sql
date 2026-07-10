-- schema.sql — Epistemic Memory pilot store.
-- Two immutable layers (events, beliefs) + operational tables around them.
-- See PLAN.md §1 for design rationale (D1: beliefs are append-only, valid_to is derived).

PRAGMA foreign_keys = ON;

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
-- First-class promises/obligations. Operational state (not a belief); every
-- transition is written to audit_traces by the caller.
CREATE TABLE IF NOT EXISTS commitments (
    id              INTEGER PRIMARY KEY,
    description     TEXT NOT NULL,
    owner           TEXT NOT NULL,
    beneficiary     TEXT NOT NULL,
    state           TEXT NOT NULL,
    deadline        TEXT,
    preconditions   TEXT,
    proof_belief_id INTEGER REFERENCES beliefs(id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ============================ artifacts + dependencies ====================
-- Derived things a correction can invalidate, and the belief edges they rest on.
CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependencies (
    id          INTEGER PRIMARY KEY,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    belief_id   INTEGER NOT NULL REFERENCES beliefs(id)
);

-- ============================ audit_traces ================================
-- One row per assembled context / gate decision / action. `explain` reads these.
CREATE TABLE IF NOT EXISTS audit_traces (
    id          INTEGER PRIMARY KEY,
    agent_id    TEXT,
    kind        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- ============================ proposals ===================================
-- --propose approval queue (M7). Approve -> runs the normal commit path.
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY,
    candidate   TEXT NOT NULL,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
