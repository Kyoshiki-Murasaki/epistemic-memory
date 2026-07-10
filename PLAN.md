# PLAN — Epistemic Memory pilot

Status: **M0–M3 complete and takeover-audited (60 tests passing). `02_SPEC.md`/`03_CLAUDE_CODE_PROMPT.md`/
`05_ADOPTION_STRATEGY.md` are mutually consistent (11 components, 11-step demo,
MemGov-Bench in success criteria). Starting M4.**
Authority order: `02_SPEC.md` is the single source of truth (success criteria section governs
the final gate); `AI Memory System.markdown` is the WHY where the spec is silent.

---

## 0. Design decisions

D1–D5 below are all **confirmed (2026-07-05)**.

### D1 — Beliefs are strictly append-only (recommended)
The success criteria forbid *UPDATE on events* and *hard-DELETE on beliefs*. I want to go one
step further and make **beliefs as immutable as events: INSERT-only, no UPDATE, no DELETE**.
Every state change — supersede, dispute, retract, verify-upgrade — is a **new belief row**
(a new version) linked via `supersedes_id`. Consequences:

- `valid_to` and "is this belief current?" are **derived** from the supersede chain at read
  time, not stored-then-mutated. (`valid_to(b)` = `valid_from` of the row that supersedes `b`,
  else null.) This is a small, deliberate deviation from the spec's literal `valid_to` column.
- Immutability becomes trivially demonstrable: SQLite triggers `RAISE(ABORT)` on any
  UPDATE/DELETE of `events` and `beliefs`, and `grep` finds zero `UPDATE beliefs` /
  `DELETE FROM beliefs` statements — the strongest possible version of the spec's headline grep.

**Alternative (spec-literal, D1-B):** keep a stored `valid_to` column; a single audited
`supersede()`/`retract()` method sets `valid_to` + `status` on the old row. Matches the spec's
column list exactly; immutability test instead asserts content columns never change and no row
is deleted. I recommend **D1 (append-only)**; say the word if you prefer D1-B.

### D2 — Supersession vs. conflict are different mechanisms
Both involve the same structural key `(entity, attribute)`, but:

- **Supersession** = a temporal update *within the same source lineage* (same `source_id`) for
  the same key. New version supersedes the old; old drops out of "current." Example: user says
  "might move to London" → later "staying in Delhi" supersedes the plan.
- **Conflict** = two *different* sources assert different values for the same key. **Both stay
  current and coexist**, flagged as conflicting; the trust matrix picks the winner *per
  decision* at assembly/gate time — it is never silently superseded. Example: user_stated "paid"
  vs. billing system_verified "FAILED" (spec step 2: "conflict detected and kept").

This matches the spec but the spec doesn't state the rule explicitly, so flagging it. It means
`current_beliefs(entity, attribute)` can return >1 row (a live conflict).

### D3 — Two tables beyond the spec's named six
Spec names `events, beliefs, sources, commitments, dependencies, audit_traces`. I add two small
supporting tables:
- `artifacts` — the derived things propagation acts on (summary / pending_action / reply), each
  with a `state`. `dependencies` stays as the spec's edge table (artifact → belief). Without
  this, an artifact's state would be duplicated across every dependency row.
- `proposals` — the `--propose` approval queue (M7). One row per queued candidate belief.

Plus a `beliefs_fts` FTS5 virtual table for M4 retrieval. All minimal; flagging for visibility.

### D4 — Extraction model id
Spec says `claude-sonnet-4-6`, which isn't a current model id. I'll default the (behind-a-flag)
live extractor to **`claude-sonnet-5`** as a single constant. Tests never call it — they use
JSON fixtures. Change the constant if you want a different model.

### D5 — Multi-agent / core-API additions — schema impact ≈ none *(confirmed)*
The refreshed `03_CLAUDE_CODE_PROMPT.md` reframes the project as a shared *foundation* and adds a
`MemoryStore` core API (the ONLY public entry; `agent_id` on every call), per-agent permissions
(M3), an MCP server (M8), a multi-agent demo step (M9), and MemGov-Bench (M11). Deliberately
minimal schema consequences:
- **Agent identity is a caller parameter, not stored state.** `MemoryStore(agent_id=...)` threads
  it through reads/gates. No `agents` table.
- **Per-agent permissions live in `trust_policy.yaml` as DATA** (a new `agents:` section, added in
  M3), evaluated by the same pure functions as the trust matrix — consistent with "policy is data."
- **One new column:** `audit_traces.agent_id`, so every assembly/gate/action is attributable to the
  agent that caused it (needed for the M8/M9 multi-agent tests and MemGov-Bench dimension 5).
- **MemGov-Bench (M11) needs no schema at all** — it's a harness driving the existing core API
  through an adapter interface; our reference adapter wraps `MemoryStore`.

The bigger M1 change is architectural, not schema: introduce `MemoryStore` as the sole public API
and enforce *no module outside `core`/`store` imports sqlite3* (verified by a test).

### Token metering without extra deps
The spec's dependency list is SQLite + stdlib + Pydantic + Anthropic + pytest + rich (no
tiktoken). Anthropic's token-count endpoint needs the network + a key, but the demo must run
without one. So per-turn "tokens injected" is a **deterministic stdlib estimator** (word +
punctuation split), clearly labelled as an estimate. Good enough to make memory *weight*
visible, which is the point.

---

## 1. Proposed SQLite schema (DDL)

```sql
-- ============================ sources =====================================
-- A source of information. `type` is the key the trust matrix reads.
CREATE TABLE sources (
    id          TEXT PRIMARY KEY,            -- 'billing_system', 'chat#88', 'agent'
    type        TEXT NOT NULL,               -- user|billing_system|crm|manager|
                                             -- third_party|document|agent_inference|untrusted_channel
    label       TEXT NOT NULL,
    created_at  TEXT NOT NULL                -- ISO-8601
);

-- ============================ events ======================================
-- Raw, immutable "what actually happened". Append-only. Never updated/deleted.
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(id),
    content     TEXT NOT NULL,               -- raw text or JSON payload
    scope       TEXT NOT NULL,               -- scope the event pertains to (see Scope)
    meta        TEXT,                        -- optional JSON (order id, channel, ...)
    created_at  TEXT NOT NULL
);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- ============================ beliefs =====================================
-- The system's versioned interpretation. Append-only (D1): a change = a new row.
CREATE TABLE beliefs (
    id            INTEGER PRIMARY KEY,
    entity        TEXT NOT NULL,             -- structural key part 1: 'order_4411'
    attribute     TEXT NOT NULL,             -- structural key part 2: 'payment_status'
    value         TEXT NOT NULL,             -- 'paid' | 'FAILED' | 'Delhi' ...
    status        TEXT NOT NULL,             -- closed EpistemicStatus enum (engine-validated)
    scope         TEXT NOT NULL,             -- 'global'|'project:<id>'|'persona'|'task_type:<t>'
    source_id     TEXT NOT NULL REFERENCES sources(id),
    event_id      INTEGER REFERENCES events(id),   -- provenance back to raw
    supersedes_id INTEGER REFERENCES beliefs(id),  -- prior version (nullable)
    decision_type TEXT,                      -- optional hint linking attr -> trust_matrix key
    valid_from    TEXT NOT NULL,             -- world-time this version holds from
    created_at    TEXT NOT NULL              -- ingest time
    -- valid_to is DERIVED (D1), not stored.
);
CREATE INDEX beliefs_key ON beliefs(entity, attribute);
CREATE TRIGGER beliefs_no_update BEFORE UPDATE ON beliefs
  BEGIN SELECT RAISE(ABORT, 'beliefs are versioned: supersede, do not edit'); END;
CREATE TRIGGER beliefs_no_delete BEFORE DELETE ON beliefs
  BEGIN SELECT RAISE(ABORT, 'beliefs are never deleted: retract via a new version'); END;

-- FTS5 over searchable belief text (M4). Kept in sync on belief insert (append-only, easy).
CREATE VIRTUAL TABLE beliefs_fts USING fts5(
    entity, attribute, value, content='beliefs', content_rowid='id'
);

-- ============================ commitments =================================
-- First-class promises/obligations. Mutable operational state (NOT a belief);
-- every transition is written to audit_traces.
CREATE TABLE commitments (
    id            INTEGER PRIMARY KEY,
    description   TEXT NOT NULL,             -- 'refund within 5 days'
    owner         TEXT NOT NULL,             -- responsible party
    beneficiary   TEXT NOT NULL,             -- who it's owed to
    state         TEXT NOT NULL,             -- open|waiting|fulfilled|cancelled|overdue
    deadline      TEXT,                      -- ISO date
    preconditions TEXT,                      -- free text / JSON ('payment system_verified')
    proof_belief_id INTEGER REFERENCES beliefs(id),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- ============================ artifacts + dependencies ====================
-- Derived things a correction can invalidate, and the belief edges they rest on.
CREATE TABLE artifacts (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,               -- summary|pending_action|reply|decision
    ref         TEXT NOT NULL,               -- human label / external ref
    state       TEXT NOT NULL,               -- active|stale|halted|needs_review
    created_at  TEXT NOT NULL
);
CREATE TABLE dependencies (
    id          INTEGER PRIMARY KEY,
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    belief_id   INTEGER NOT NULL REFERENCES beliefs(id)
);

-- ============================ audit_traces ================================
-- One row per assembled context / gate decision / action. `explain` reads these.
CREATE TABLE audit_traces (
    id          INTEGER PRIMARY KEY,
    agent_id    TEXT,                        -- which agent caused this trace (multi-agent audit)
    kind        TEXT NOT NULL,               -- assembly|gate|action|commitment|propagation
    summary     TEXT NOT NULL,               -- human-readable one-liner
    payload     TEXT NOT NULL,               -- JSON: injected belief ids+statuses, conflicts +
                                             -- resolving rule, gate decision+rule, counterfactual,
                                             -- tokens_injected
    created_at  TEXT NOT NULL
);

-- ============================ proposals ===================================
-- --propose approval queue (M7). Approve -> runs the normal commit path.
CREATE TABLE proposals (
    id          INTEGER PRIMARY KEY,
    candidate   TEXT NOT NULL,               -- JSON CandidateBelief + source/event/scope
    state       TEXT NOT NULL,               -- pending|approved|rejected
    created_at  TEXT NOT NULL
);
```

Ephemeral (no-write) sessions need no schema: a runtime flag makes every commit path a logged
no-op while retrieval still works.

---

## 2. Proposed Pydantic v2 models (`epistemic_memory/models.py`)

```python
from enum import Enum
from typing import Optional
from pydantic import BaseModel

class EpistemicStatus(str, Enum):
    mentioned = "mentioned"
    user_stated = "user_stated"
    third_party_stated = "third_party_stated"
    ai_inferred = "ai_inferred"
    considering = "considering"
    planned = "planned"
    promised = "promised"
    corroborated = "corroborated"
    system_verified = "system_verified"
    disputed = "disputed"
    superseded = "superseded"
    retracted = "retracted"
    do_not_use = "do_not_use"

class RiskTier(str, Enum):           # ordered informational < ... < irreversible
    informational = "informational"
    low_stakes = "low_stakes"
    high_stakes = "high_stakes"
    irreversible = "irreversible"

class GateDecision(str, Enum):
    allow = "allow"
    deny = "deny"
    needs_human = "needs_human"

class CommitmentState(str, Enum):
    open = "open"; waiting = "waiting"; fulfilled = "fulfilled"
    cancelled = "cancelled"; overdue = "overdue"

# Scope is validated structurally: 'global' | 'project:<id>' | 'persona' | 'task_type:<t>'
class Scope(BaseModel):
    kind: str            # global | project | persona | task_type
    ref: Optional[str] = None
    # .parse('project:hobby') / .render() -> 'project:hobby' / .matches(task_scope) helpers

class Source(BaseModel):
    id: str; type: str; label: str; created_at: str

class Event(BaseModel):
    id: Optional[int] = None
    source_id: str; content: str; scope: str
    meta: Optional[dict] = None; created_at: str

class Belief(BaseModel):
    id: Optional[int] = None
    entity: str; attribute: str; value: str
    status: EpistemicStatus; scope: str
    source_id: str; event_id: Optional[int] = None
    supersedes_id: Optional[int] = None
    decision_type: Optional[str] = None
    valid_from: str; created_at: str
    @property
    def key(self) -> tuple[str, str]: return (self.entity, self.attribute)

# What the LLM PROPOSES from an event (never committed as-is).
class CandidateBelief(BaseModel):
    entity: str; attribute: str; value: str
    proposed_status: EpistemicStatus
    scope: str
    decision_type: Optional[str] = None

class Commitment(BaseModel):
    id: Optional[int] = None
    description: str; owner: str; beneficiary: str
    state: CommitmentState; deadline: Optional[str] = None
    preconditions: Optional[str] = None
    proof_belief_id: Optional[int] = None
    created_at: str; updated_at: str

class Artifact(BaseModel):
    id: Optional[int] = None
    kind: str; ref: str; state: str; created_at: str

# --- policy models (loaded from trust_policy.yaml) ---
class TrustRule(BaseModel):
    rule_id: str; authoritative: list[str]; ranking: list[str]

class GateRule(BaseModel):
    min_status: EpistemicStatus
    require_authoritative_source: bool
    require_uncontradicted: bool
    require_current: bool

class ActionSpec(BaseModel):
    risk: RiskTier; decision: str; require_value: Optional[str] = None

class TrustPolicy(BaseModel):
    version: int
    status_strength: dict[str, int]
    source_status_ceiling: dict[str, EpistemicStatus]
    trust_matrix: dict[str, TrustRule]
    gate_rules: dict[str, GateRule]
    actions: dict[str, ActionSpec]

# --- outputs ---
class ConflictResolution(BaseModel):
    winner: Belief; losers: list[Belief]
    rule_id: str; contradicted: bool

class GateResult(BaseModel):
    decision: GateDecision
    reasons: list[str]          # human-readable why-chain

class ReceiptLine(BaseModel):
    belief_id: int; status: EpistemicStatus
    source_id: str; scope: str; admitted_by: str   # policy rule / filter

class AssembledContext(BaseModel):
    text: str                   # the rendered epistemic block + permissions block
    receipt: list[ReceiptLine]
    tokens_injected: int
    conflicts: list[str]
```

---

## 2b. Core API — `MemoryStore` (M1, the ONLY public entry)

Every client (CLI, MCP server, demo, benchmark adapter) goes through this; nothing else touches
SQLite. `agent_id` is threaded into every read/gate so per-agent permissions (M3) apply. Method
names match the spec's core-API verbs (line 23) 1:1, since M8 maps each straight onto an MCP
tool (`memory_<verb>`, with `assemble`→`memory_assemble_context` and `gate`→`memory_gate_action`):

```python
class MemoryStore:
    def __init__(self, db_path, policy, *, agent_id, ephemeral=False, propose=False): ...

    # ingest (M2) — append raw event, extract+validate+commit beliefs (or queue if propose=True)
    def ingest(self, *, source_id, content, scope, meta=None) -> IngestResult: ...

    # retrieve (M4) — agent_id-scoped, scope-filtered ranked beliefs; no rendering, no gating
    def retrieve(self, *, query, entity=None, scope, task_type=None,
                 status_floor=None) -> list[Belief]: ...

    # assemble (M4) — retrieve() + epistemic headers, conflict flags, permissions block,
    # dedup, token budget/metering, memory receipt
    def assemble(self, *, query, entity=None, scope, task_type=None,
                 token_budget=1024) -> AssembledContext: ...

    # gate (M3/M4) — the action gate; never bypassable; respects the agent's
    # action tier and the active task/agent scope intersection. Missing scope
    # fails closed. decision_type remains policy-derived from the action.
    # decision_type is NOT a param: derived from policy.actions[action].decision (by
    # convention in this pilot, decision_type == the belief attribute name), so a
    # caller can never pass a decision_type inconsistent with the action itself.
    def gate(self, *, action, entity, scope, task_type=None) -> GateResult: ...

    # correct (M6) — invalidate/supersede a belief and propagate through dependencies
    def correct(self, belief_id, *, reason) -> PropagationReport: ...

    # explain (M7) — why-chain for a trace id
    def explain(self, trace_id) -> str: ...

    # commitments (M5) — not one of the 6 spec verbs, but part of the same public surface
    def add_commitment(self, ...) -> Commitment: ...
```

`store.py` holds the SQLite DAL (the only other module allowed to import sqlite3); `core.py`'s
`MemoryStore` composes ingest/policy/retrieve/assemble/gatekeeper/propagate/audit over it.

---

## 3. Milestones (each ends with its verification actually run)

- **M0 — scaffold.** pyproject, README stub, `.gitignore`, `trust_policy.yaml`, this PLAN.
  → verify: `python -c "import yaml,pathlib; yaml.safe_load(open('trust_policy.yaml'))"` loads;
  files present. **(done)**

- **M1 — core + store.** `models.py`; `store.py` = SQLite DAL (`add_source`, `add_event`,
  `add_belief`, `supersede`, `current_beliefs`, chain walk, immutability triggers); `core.py` =
  the `MemoryStore` public API (`agent_id` on every call). Only `store.py` + the schema loader
  import sqlite3.
  → verify: `pytest tests/test_store.py` — event UPDATE/DELETE raise, belief DELETE raises,
  supersede creates a new version with the old still queryable; **a test asserts no module
  outside `core`/`store` imports sqlite3**.

- **M2 — ingest.** `ingest.py`: append event → LLM extracts `CandidateBelief`s + proposed
  status → engine validates against the closed enum, clamps to `source_status_ceiling`, applies
  D2 (supersede vs. conflict), commits. Live extractor behind `--live` flag / `ANTHROPIC_API_KEY`;
  fixtures otherwise.
  → verify: `pytest tests/test_ingest.py` — fixture events produce expected beliefs; a
  proposed `system_verified` from a `user` source is clamped to `user_stated`.

- **M3 — policy engine + per-agent permissions.** `policy.py`: pure
  `resolve_conflict(beliefs, decision_type)` and `gate(action, supporting_beliefs, agent_id)` over
  the four tiers; adds an `agents:` section to `trust_policy.yaml` (allowed scopes + max action
  tier per agent). No I/O, no DB.
  → verify: `pytest tests/test_policy.py` — exhaustive table (refund denied, confirm_payment
  denied, ask_for_receipt allowed, injected credit denied, name update allowed) **plus** a
  read-only agent blocked from a high-stakes action by permission.

- **M4 — retrieval + assembly (the heart).** `retrieve.py` (FTS5 + entity/status-floor/scope
  filters, ranking = relevance × status_strength × recency, retrieval-time dedup) and
  `assemble.py` (epistemic headers, conflict flags, permissions block, token budget + metering,
  memory receipt).
  → verify: `pytest tests/test_retrieve.py tests/test_assemble.py` — snapshot of rendered
  blocks; pixel-art scope-leak excluded; duplicates collapsed; `tokens_injected` reported.

- **M5 — commitments.** `commitments.py` + state machine; overdue surfacing against a fixed
  demo clock.
  → verify: `pytest tests/test_commitments.py` — open→waiting→fulfilled and →overdue transitions.

- **M6 — propagation.** `propagate.py`: on invalidation, walk `dependencies`, mark summaries
  stale, halt pending actions, report executed ones.
  → verify: `pytest tests/test_propagate.py` — correcting a belief flips its summary to `stale`
  and a pending action to `halted`, and lists them.

- **M7 — audit + trust modes.** `audit.py` (`explain(trace_id)`), `--propose` approval queue,
  ephemeral no-write sessions. No silent commits anywhere.
  → verify: `pytest tests/test_audit.py tests/test_trust_modes.py` — `explain` prints the
  why-chain; `--propose` leaves nothing committed until approval; ephemeral writes nothing.

- **M8 — MCP server.** `mcp_server.py`: expose the `MemoryStore` API as MCP tools with per-agent
  identity, using the official `mcp` SDK (added as an `epistemic-memory[mcp]` extra so the demo
  stays key/dep-light).
  → verify: `pytest tests/test_mcp.py` — server starts, tools list, and a scripted second agent
  with read-only/informational permissions is correctly limited by policy.

- **M9 — demo (showpiece).** `demo/__main__.py`: the full **11-step** scenario with `rich`
  output, memory receipts, token counts — the 10 support steps + a multi-agent permission step,
  including the scope-leak and injection tests.
  → verify: `python -m demo` exits 0; `pytest tests/test_demo.py` asserts key lines.

- **M10 — README.** open with the spec's mission statement verbatim, then the pitch, the
  governance comparison table vs Mem0/Zep/Letta (incl. multi-agent permissions), MCP quick-start
  (3 commands), demo GIF/asciinema instructions, and a contributor-facing roadmap on the core API.
  → verify: manual read; links resolve; comparison table + MCP quick-start present.

- **M11 — MemGov-Bench (the category play; see `05_ADOPTION_STRATEGY.md`).** `memgov_bench/`:
  runnable `python -m memgov_bench --adapter ours` scoring five dimensions (stale-fact leakage,
  claim/fact confusion, scope leakage, injection resistance, gate correctness) via deterministic
  scripted scenarios, pass/fail, **run 3× with variance reported**. Adapter interface + reference
  adapter wrapping `MemoryStore`; Mem0/Letta adapters are stretch (stub cleanly — "cannot express
  a dimension" is itself a reportable result). Output: a markdown scores table the README embeds.
  → verify: `python -m memgov_bench --adapter ours` prints the table and exits 0;
  `pytest tests/test_memgov_bench.py` asserts our adapter passes all five dimensions.

**Final gate:** run the entire `02_SPEC.md` "Success criteria" section and paste output —
full `pytest`, `python -m demo` exit 0, the immutability grep returns nothing, and every wrong
demo answer traces via `explain`.

---

## 4. Explicitly out of scope (roadmap, per spec)
Embeddings / semantic search, multi-tenant auth, real billing/CRM connectors (JSON fixtures
only), UI beyond a read-only inspector, performance-at-scale, learned trust scores.
