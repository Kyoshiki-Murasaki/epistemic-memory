# PLAN — Epistemic Memory pilot

Status: **M0–M11 complete. M11 adds the deterministic five-dimension MemGov-Bench self-evaluation
through the public `MemoryStore` boundary, ten independently labelled synthetic cases, exact
three-run scoring and variance, and focused validity/reproducibility checks. Later milestones are
not implemented.**
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

### D3 — Artifact and dependency tables
Spec names `events, beliefs, sources, commitments, dependencies, audit_traces`. I add two small
supporting tables:
- `artifacts` — the derived things propagation acts on, separating `kind` (`output|action`),
  execution history (`not_applicable|pending|executed`), and propagation state
  (`current|stale|halted|review_required`). Stable identity, scope, creator, label/reference,
  execution history, and creation time are immutable; artifacts cannot be deleted.
- `dependencies` — explicit belief→artifact and artifact→artifact edges. Foreign keys prevent
  dangling endpoints, partial unique indexes prevent duplicate edges, and the M6 public API
  enforces scope compatibility, no self-edges, and a DAG invariant.
- `proposals` — the `--propose` approval queue (M7). One row per queued candidate belief.

Plus a `beliefs_fts` FTS5 virtual table for M4 retrieval. All minimal; flagging for visibility.

### D4 — Extraction model id
The optional live extractor and spec use **`claude-sonnet-5`** as a single constant. Tests never
call it; deterministic fixtures cover the default test, demo, and benchmark paths.

### D5 — Multi-agent / core-API additions — schema impact ≈ none *(confirmed)*
The refreshed `03_CLAUDE_CODE_PROMPT.md` reframes the project as a shared *foundation* and adds a
`MemoryStore` core API (the ONLY public entry; `agent_id` on every call), per-agent permissions
(M3), an MCP server (M8), a multi-agent demo step (M9), and MemGov-Bench (M11). Deliberately
minimal schema consequences:
- **Agent identity is a caller parameter, not stored state.** `MemoryStore(agent_id=...)` threads
  it through reads/gates. No `agents` table.
- **Per-agent permissions live in `trust_policy.yaml` as DATA** (a new `agents:` section, added in
  M3), evaluated by the same pure functions as the trust matrix — consistent with "policy is data."
- **Correction provenance is exact-source authorized.** `CorrectionRequest` cannot select a
  source. Each agent has explicit `writable_source_ids`, validated against policy-declared
  source principals whose expected runtime source type is also bound in policy.
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
    id                    INTEGER PRIMARY KEY,
    kind                  TEXT NOT NULL,      -- output|action
    execution_state       TEXT NOT NULL,      -- not_applicable|pending|executed
    propagation_state     TEXT NOT NULL,      -- current|stale|halted|review_required
    scope                 TEXT NOT NULL,
    label                 TEXT NOT NULL,
    reference             TEXT,
    created_by_agent_id   TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE TABLE dependencies (
    id                       INTEGER PRIMARY KEY,
    upstream_belief_id       INTEGER REFERENCES beliefs(id),
    upstream_artifact_id     INTEGER REFERENCES artifacts(id),
    downstream_artifact_id   INTEGER NOT NULL REFERENCES artifacts(id),
    created_by_agent_id      TEXT NOT NULL,
    created_at               TEXT NOT NULL
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

Ephemeral sessions need no additional schema. The host selects `SessionMode.ephemeral`, the
existing database opens with storage-level read-only enforcement, writes return typed denials,
and answer/action traces remain instance-local rather than durable.

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
    id: int; kind: ArtifactKind; execution_state: ArtifactExecutionState
    propagation_state: ArtifactPropagationState; scope: str
    label: str; reference: Optional[str]; created_by_agent_id: str
    created_at: datetime; updated_at: datetime

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
    def __init__(self, db_path, policy, *, agent_id,
                 session_mode=SessionMode.direct, session_id=None,
                 approval_actor_id=None, live=False, clock=None,
                 id_factory=None, trusted_sources=None): ...

    # ingest (M2) — append raw event, extract+validate+commit beliefs (or queue if propose=True)
    def ingest(self, *, source_id, content, scope, meta=None) -> IngestResult: ...

    # retrieve (M4) — agent_id-scoped, scope-filtered ranked beliefs; no rendering, no gating
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...

    # assemble (M4) — retrieve() + epistemic headers, conflict flags, permissions block,
    # dedup, token budget/metering, memory receipt
    def assemble(self, request: AssemblyRequest) -> AssembledContext: ...

    # gate (M3/M4) — the action gate; never bypassable; respects the agent's
    # action tier and the active task/agent scope intersection. Missing scope
    # fails closed. decision_type remains policy-derived from the action.
    # decision_type is NOT a param: derived from policy.actions[action].decision (by
    # convention in this pilot, decision_type == the belief attribute name), so a
    # caller can never pass a decision_type inconsistent with the action itself.
    def gate(self, *, action, entity, scope=None, task_type=None) -> GateResult: ...

    # M6 — explicit artifact graph registration and atomic correction propagation
    def register_artifact(self, request: ArtifactRegistrationRequest
                          ) -> ArtifactRegistrationResult: ...
    def register_dependency(self, request: DependencyRegistrationRequest
                            ) -> DependencyRegistrationResult: ...
    def correct(self, request: CorrectionRequest) -> CorrectionResult: ...

    # explain (M7) — why-chain for a trace id
    def explain(self, request: ExplainRequest) -> ExplainResult: ...

    # commitments (M5) — typed operations on the same public service boundary
    def add_commitment(
        self, request: CommitmentCreateRequest
    ) -> CommitmentMutationResult: ...
    def transition_commitment(
        self, request: CommitmentTransitionRequest
    ) -> CommitmentMutationResult: ...
    def list_commitments(
        self, request: CommitmentListRequest
    ) -> CommitmentListResult: ...
    def surface_overdue(
        self, request: OverdueScanRequest
    ) -> OverdueScanResult: ...
```

`store.py` holds the SQLite DAL (the only other module allowed to import sqlite3); `core.py`'s
`MemoryStore` composes ingest/policy/retrieve/assemble/gatekeeper/propagate/audit over it.

---

## 3. Milestones (each ends with its verification actually run)

- **M0 — scaffold.** pyproject, README stub, `.gitignore`,
  `epistemic_memory/trust_policy.yaml`, this PLAN.
  → verify:
  `python -c "import yaml,pathlib; yaml.safe_load(open('epistemic_memory/trust_policy.yaml'))"`
  loads;
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

- **M5 — commitments. (done)** `commitments.py` owns the single transition table;
  `created_by_agent_id` is immutable and separate from the domain `owner`; policy grants
  explicit `create`, `transition`, and `scan_overdue` operations. Manual transitions are
  creator-bound. Overdue promotion requires `scan_overdue`, uses an explicit aware-UTC
  the aware-UTC time supplied by an injectable `MemoryStore` clock, promotes every authorized
  eligible record only when authoritative time is strictly greater than `deadline`, and is
  idempotent. Creation and manual-transition lifecycle timestamps use that same boundary;
  public requests contain no lifecycle timestamp. Reading remains governed only by the existing
  task/agent scope intersection.
  → verified: `.venv/bin/python -m pytest -vv tests/test_commitments.py` — **55 passed**;
  full suite — **142 passed**.

- **M6 — propagation. (done)** `propagate.py`: explicit policy-controlled artifact and DAG
  registration; same-source append-only correction/retraction through the clamped ingest path;
  cycle-safe deterministic transitive traversal; outputs become stale, pending actions halt,
  and executed actions remain executed but require review. Correction creation and every
  reachable artifact update share one SQLite transaction and one store-clock sample. Safety
  propagation crosses response-visibility boundaries while the typed result exposes hidden
  effects only as safe counts/rule codes. The M6 security audit removes caller-selected source
  identity, requires exact source-ID/type write authority before ingest, rejects invalid belief
  or artifact endpoints at dependency registration, and separates dependency usability from
  correction eligibility so current disputed/retracted/do-not-use beliefs remain correctable.
  → verified: `.venv/bin/python -m pytest -vv tests/test_propagation.py` — **51 passed**;
  full suite — **193 passed**.

- **M7 — audit + trust modes.** `audit.py` (`explain(trace_id)`), `--propose` approval queue,
  ephemeral no-write sessions. No silent commits anywhere.
  → verify: `pytest tests/test_audit.py tests/test_proposals.py tests/test_ephemeral.py` —
  `explain` returns the why-chain; proposal mode commits no belief until approval; ephemeral
  operation leaves application state unchanged.

- **M8 — MCP server.** `mcp_server.py`: expose the `MemoryStore` API as MCP tools with per-agent
  identity, using the official `mcp` SDK as a runtime dependency.
  → verify: `pytest tests/test_mcp_server.py` — server starts, tools list, and a scripted second agent
  with read-only/informational permissions is correctly limited by policy.

- **M9 — deterministic demo. (done)** `epistemic_memory/demo.py` runs the binding **11-step**
  scenario through `MemoryStore`, using a fixed aware-UTC mutable clock, deterministic ID
  factory, exact policy-bound source catalog, fixture extractors, and a fresh temporary SQLite
  database by default. It has no live model call, network call, sleep, or wall-clock input. The
  transcript uses stable plain text plus the existing safe renderer; every step asserts typed
  results and persisted outcomes before printing success. A constructor-only `trusted_sources`
  catalog lets the trusted host initialize exact source principals without exposing source setup
  in requests or importing `Store`/SQLite in production demo code. Proposal, ephemeral, and
  official stdio MCP proofs remain the existing implementation paths, not demo-specific copies.
  → run: `.venv/bin/python -m epistemic_memory.demo` or `.venv/bin/epistemic-memory-demo`;
  optional `--db PATH` creates only a new explicit database and refuses an existing path.
  → verified: `.venv/bin/python -m pytest -vv tests/test_demo.py` — **28 passed**; two complete
  runs have byte-identical transcripts; module and console entry points expose side-effect-free
  `--help`.

### M9 canonical step mapping

The quoted step descriptions below are the current `docs/02_SPEC.md` sequence. Each row records
the public call, state/audit effect, visible proof, and security invariant used by the demo.

| Step | Binding scenario | Public API and expected state/audit | Visible proof and security invariant |
|---:|---|---|---|
| 1 | Customer says “I already paid for order 4411” | `ingest` appends one event and one clamped `user_stated` belief plus ingest trace | belief status/source/scope; requests cannot supply identity or mode |
| 2 | Billing says payment FAILED | denied support-agent `ingest` changes no file state; authorized billing `ingest` appends `system_verified` evidence and trace | both current IDs; billing impersonation is rejected before extraction; cross-source conflict is not supersession |
| 3 | Agent drafts a reply | `assemble` and three `gate` calls persist decision traces | both beliefs, P-12 conflict winner, receipt tokens, informational allow, confirmation deny; retrieval never grants action permission |
| 4 | Refund is requested | `gate(issue_refund)` persists an irreversible denial trace | policy-derived decision type/tier, G-IRREVERSIBLE and P-12, structured missing-value reason |
| 5 | Company promises refund within 5 days | `add_commitment`, `list_commitments`, and deterministic `surface_overdue` calls create and promote one commitment with mutation traces | owner/beneficiary/deadline/creator; exact deadline stays open, strictly later becomes overdue, repeat scan promotes zero |
| 6 | London move is considered, then staying in Delhi | three `ingest` calls plus `retrieve`; the relocation-plan replacement points to the old plan while current-city remains Delhi | distinct plan/reality keys and IDs; append-only same-source supersession |
| 7 | A belief-backed summary is corrected | `register_artifact`, `register_dependency`, pre-correction `gate`, authorized `correct`, and `explain`; replacement plus all propagation changes are atomic and audited | stale output, halted pending action, executed/review-required action, hidden safe count, unchanged historical trace, changed counterfactual |
| 8 | Pixel-art memory meets a banking task | scoped `ingest`, banking `assemble`/`explain`, and hobby `retrieve` | marker absent from full banking serialization and token weight; safe exclusion count only; authorized hobby retrieval succeeds |
| 9 | Untrusted credit is planted | untrusted `ingest` clamps to `mentioned`; `gate(apply_credit)` denies; propose-mode `ingest`, approval/rejection, drift failure, listing, and self-approval denial exercise proposal traces | low status and denial; proposal fields immutable; zero pre-approval belief, one clamped approved belief, none rejected/stale, agent cannot self-approve |
| 10 | Analytics bot connects to the same store | official stdio client lists/calls the six MCP tools; ephemeral `retrieve`, `assemble`, `gate`, `explain`, and blocked mutations reuse `MemoryStore` | receipt-bearing read succeeds, tier ceiling denies refund, schemas omit trusted controls, file hash is unchanged, transient trace disappears after restart |
| 11 | Explain prints the refund why-chain | `explain` reads the immutable refund and pre-correction traces with belief-removal counterfactuals | historical evidence, conflict, rule IDs, policy fingerprint, current follow-up, and conflict-versus-supersession distinction |

M9 intentionally does not add a general CLI, scheduler, remote MCP transport, UI, benchmark,
live connector, or README launch material. The MCP smoke normalizes its transient identifiers out
of the transcript; the canonical direct/propose/ephemeral paths use the fixed demo clock and IDs.

- **M10 — release documentation and launch readiness. (done)** `README.md` now opens with the
  binding mission verbatim and is the authoritative entry point for capabilities, the deterministic
  demo/hash, clean installation, exact six-tool stdio MCP setup/reference, the Python API, session
  modes, policy, architecture/trust boundaries, threat model, restrained Mem0/Zep/Letta category
  positioning, limitations, and contribution paths. `examples/trust_policy.yaml` is a separate
  least-privileged one-source policy; `examples/bootstrap_store.py`,
  `examples/python_quickstart.py`, and `examples/mcp_config.json` make local/MCP workflows
  reproducible without secrets, test-only helpers, direct SQLite access, or a live service.
  `tests/test_docs.py` adds 26 durable checks for mission/metadata/commands, demo hash and generated
  excerpt, exact MCP schemas and host-only controls, example parsing/execution, policy semantics,
  architecture/import boundaries, capability labels, forbidden claims/paths/secrets/benchmark
  results, local links, code fences/shell syntax, and a dependency-resolving temporary install
  that exposes both scripts. At M10, `pyproject.toml` required no change. The final publication
  audit later upgraded this regression to a wheel-style install, declared the schema and packaged
  demo policy as package data, and removed the unused `rich` dependency and package glob.
  → verified: focused docs **26 passed**; demo **28 passed**; MCP **13 passed**; audit/proposals/
  ephemeral **60 passed**; propagation/commitments **106 passed**; policy/retrieve/assemble/ingest
  **82 passed**; full suite **329 passed**. Two module demos and the console demo were byte-identical
  at SHA-256 `4b49f0a69cb03bf8396feca897ce3e153087eba43b8a86b19874995db7c58fcc`.
  Module/script `--help`, example execution, YAML semantic validation, JSON parsing, local-link and
  forbidden-content checks, six-tool/schema consistency, SQLite/MCP boundary checks, `compileall`,
  `pip check`, `git diff --check`, and a clean temporary venv install all passed.

### M10 canonical deliverable map

| Requirement | Implementation/source of truth | Verification evidence |
|---|---|---|
| Mission, pitch, status, concepts, limitations | `README.md`; `docs/02_SPEC.md`; accepted M1–M9 code/tests | mission/status/claim/link tests; full suite |
| Demo instructions, excerpt, hash, recording | `README.md`; `epistemic_memory/demo.py`; `tests/test_demo.py` | two byte-identical module runs, console run, hash/excerpt test |
| Five-minute install and release metadata | `README.md`; `pyproject.toml` | fresh dependency-resolving venv, both scripts, `pip check` |
| Three-step MCP setup and six-tool reference | `README.md`; `examples/mcp_config.json`; `mcp_server.py` | official-client MCP tests plus schema/name consistency checks |
| Python, proposal, ephemeral, correction examples | `examples/python_quickstart.py`; public `MemoryStore` models/methods | executable example and no-private-boundary static test |
| Least-privileged policy guidance/bootstrap | `examples/trust_policy.yaml`; `examples/bootstrap_store.py`; `policy.py` | semantic load, exact-authority assertions, idempotent bootstrap |
| Architecture and threat model | `README.md`; `core.py`; `store.py`; `mcp_server.py` | AST/import, read-only SQLite, and adapter-boundary checks |
| Category comparison and contribution path | `README.md`; `docs/04_USER_RESEARCH.md`; `docs/05_ADOPTION_STRATEGY.md` | no unsupported/superiority/benchmark-result claim checks |
| Documentation and release hygiene | `tests/test_docs.py`; this M10 record | 26 focused checks; 329-test full suite; clean diff checks |

M10 deliberately left the pilot's known boundaries intact at that checkpoint: local SQLite/FTS5,
one-process operation, stdio-only MCP, host-controlled identity, optional live extraction, no
hosted auth, remote transport, scheduler, UI, telemetry, cryptographic tamper evidence, declared
license, or benchmark. M11 adds only the finite local benchmark described below.

- **M11 — MemGov-Bench (the category play; see `05_ADOPTION_STRATEGY.md`). (done)** `memgov_bench/`:
  runnable `python -m memgov_bench --adapter ours` scoring five dimensions (stale-fact leakage,
  claim/fact confusion, scope leakage, injection resistance, gate correctness) via deterministic
  scripted scenarios, pass/fail, **run 3× with variance reported**. Adapter interface + reference
  adapter wrapping `MemoryStore`; Mem0/Letta adapters are stretch (stub cleanly — "cannot express
  a dimension" is itself a reportable result). Output: a markdown scores table the README embeds.
  → verify: `python -m memgov_bench --adapter ours` prints the table and exits 0;
  `pytest tests/test_memgov_bench.py` asserts our adapter passes all five dimensions.

  The canonical implementation contains two static synthetic cases per dimension. Every case
  compares a typed observation with a frozen fixture expectation; the adapter never reads the
  expected label. Each case gets fresh temporary state, a fixed UTC clock, deterministic IDs, and
  only public `MemoryStore` calls. A case mismatch is a benchmark failure; malformed observations
  or execution faults are harness errors. Dimensions require all cases, overall requires every
  dimension in all three runs, and scores are never averaged into a passing overall result.

  Three complete self-evaluation runs each passed 10/10 cases: every dimension was 2/2, mean/min/
  max were 100.00%, variance was 0.0000 percentage-points squared, and no correctness outcome
  differed. The minimal always-deny control failed all five dimensions. Mem0/Letta remain stretch
  adapters; no vendor integration, model trial, remote transport, publication, performance score,
  or later-milestone work was added. This is finite deterministic conformance evidence, not
  population-level statistical or production-readiness evidence.

  → verified: focused benchmark **26 passed**; full suite **355 passed**. Three separate canonical
  CLI invocations were byte-identical at SHA-256
  `ebacd6df735e81a51585500873b2703576d45f19d39c3321017959752db4884f`; each invocation recorded
  three identical 10/10 runs with zero variance. A dependency-resolving isolated editable install,
  isolated benchmark run, `pip check`, packaged policy validation, reordered cases, invalid
  fixtures, negative-control sensitivity, exact six MCP tools, SQLite/import/static boundary
  scans, `compileall`, `git diff --check`, and the unchanged M9 demo SHA-256
  `4b49f0a69cb03bf8396feca897ce3e153087eba43b8a86b19874995db7c58fcc` all passed.

**Final gate:** run the entire `02_SPEC.md` "Success criteria" section and paste output —
full `pytest`, `python -m epistemic_memory.demo` exits 0, the immutability grep returns nothing,
and every wrong demo answer traces via `explain`.

---

## 4. Explicitly out of scope (roadmap, per spec)
Embeddings / semantic search, multi-tenant auth, real billing/CRM connectors (JSON fixtures
only), UI beyond a read-only inspector, performance-at-scale, learned trust scores.
