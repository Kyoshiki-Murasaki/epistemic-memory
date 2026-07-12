# SPEC — Epistemic Memory: a trust-and-governance memory layer for AI agents

This spec turns the vision document ([`AI Memory System.markdown`](AI%20Memory%20System.markdown))
into a concrete, buildable pilot.
The vision doc is the WHY. This file is the WHAT. Read both before writing code.

## Mission (this is the project's north star — put it verbatim in the README)

Build a governed memory foundation for AI: the thing developers use when they
need AI systems to remember *safely*, not just remember more. This memory does not treat
everything as a fact. It knows where information came from, how certain it is, where it is
allowed to be used, whether it is still current, what disagrees with it, and whether it is
strong enough to support an answer or action. The goal: AI memory that is trustworthy,
inspectable, correctable, and usable across many agents and tools — a common memory layer
other systems build on, instead of every AI app inventing its own unsafe memory.

## One-sentence pitch

Existing memory systems answer "what do I remember?" — this foundation also answers
"why should I trust it, am I allowed to use it here, and is it strong enough to act on?"

## Foundation requirements (what "a layer others build on" means concretely)

1. **A small, stable core API.** One `MemoryStore` interface (ingest, retrieve, assemble,
   gate, correct, explain) with typed Pydantic models. Everything else — CLI, demo, MCP
   server — is a thin client of this interface. No client may bypass it to touch SQLite.
2. **An MCP server** exposing the core as tools (`memory_ingest`, `memory_retrieve`,
   `memory_assemble_context`, `memory_gate_action`, `memory_explain`, `memory_correct`), so
   any MCP-capable agent (Claude Code, IDE agents, custom agents) can share one governed
   memory. This is the "usable across many agents and tools" claim, shipped.
3. **Per-agent identity and permissions.** Every caller has an `agent_id`; the trust policy
   can scope what each agent may read and which action tiers it may request. Two agents
   sharing one store with different permissions is a demo step, not a roadmap item.
4. **The storage backend is swappable in principle, SQLite in practice.** Keep the store
   behind the interface so Postgres can come later, but build ONLY SQLite now.

## Core design principles (non-negotiable)

1. **Two layers, never merged.**
   - **Event log (immutable):** every raw input — user messages, system records, agent
     inferences — is appended and never edited or deleted. This is "what actually happened."
   - **Belief layer (versioned):** the system's current interpretation of the events. Beliefs
     can be superseded, disputed, retracted — but never silently rewritten. A new version is
     created and linked to the old one with a `supersedes` pointer. History is always walkable.

2. **Every belief carries epistemic status.** Status is a closed enum, not free text:
   `mentioned | user_stated | third_party_stated | ai_inferred | considering | planned |
   promised | corroborated | system_verified | disputed | superseded | retracted | do_not_use`
   Nothing is ever stored as a bare "fact." "Customer says they paid" and "billing confirms
   payment" are two different beliefs with two different statuses that can coexist and conflict.

2b. **Supersession is structural, not similarity-based.** Every belief carries a structural
   key `(entity, attribute)` (e.g., `(customer_881, current_city)`), extracted by the LLM but
   validated by the engine. A newer different value from the same source supersedes the older
   same-key belief. Cross-source disagreement remains live and is resolved by explicit policy.
   Contradiction handling is structural rather than embedding-similarity based; version history
   remains walkable through the supersession chain.

2c. **Every belief carries a scope.** `global | project:<id> | persona | task_type:<t>`.
   Context assembly filters by scope: a pixel-art preference scoped to the user's hobby
   project must never be injected into their banking-website task.

3. **Trust is per-decision, not per-source.** A YAML policy file defines a trust matrix:
   which source types are authoritative for which decision types. Billing system outranks the
   user for `payment_status`; the user outranks everyone for `preferred_name`. The policy is
   data, not code — a company can edit it without touching the engine.

4. **Retrieval ≠ permission.** Finding a memory never grants the right to act on it. A
   separate **action gate** checks, per action risk tier, whether the supporting beliefs are
   current, uncontradicted, from an authorized source, and strong enough. Risk tiers:
   `informational < low_stakes < high_stakes < irreversible`. An unverified user claim can
   justify a clarifying question (informational) but never a refund (irreversible).

5. **Commitments are first-class, not facts.** Promises, obligations, and unfinished work get
   their own table with a state machine (`open → waiting → fulfilled | cancelled | overdue`),
   owner, beneficiary, deadline, preconditions, and proof-of-completion link. They surface
   proactively; they don't rot inside a list of memories.

6. **Corrections propagate.** Every registered derived artifact (summary, decision, or action)
   stores the IDs of the beliefs or upstream artifacts it depends on. When a belief is invalidated,
   the system walks this dependency graph and: marks dependents stale, halts pending actions
   built on it, and lists already-executed actions that may need human review.

7. **Durable answer, action, and write paths are auditable.** Context assembly, action gating,
   and domain mutations write immutable traces with their decision-time evidence and policy.
   Raw retrieval is intentionally untraced because it does not authorize an answer or action.

## The LLM-facing insight (the most important implementation detail)

LLMs treat injected context as ground truth unless told otherwise. So the **context assembly
layer** is where this system wins or loses:

- Never inject a bare fact. Every belief is rendered with an inline epistemic header:
  `[user_stated · unverified · 2026-06-28 · source: chat#88] "I already paid for order 4411"`
  `[system_verified · 2026-07-04 · source: billing] Order 4411 payment status: FAILED`
- **Conflicts are injected together and flagged**, never silently resolved by dropping one:
  `⚠ CONFLICT (payment_status): billing outranks user_stated per policy P-12.`
- The assembled context ends with a short **permissions block** telling the model what the
  retrieved information licenses it to do (e.g., "You may acknowledge the claim and ask for a
  receipt. You may NOT confirm payment or trigger a refund — gate requires system_verified.").
- Rank beliefs by (task relevance × status strength × recency) and respect a token budget.
  Injecting everything is as bad as injecting nothing.
- **The store is authoritative on status.** The LLM may *propose* a status change ("this looks
  corroborated now"), but only the engine — applying policy, or a human — commits it. The
  model never upgrades a claim to a fact on its own.
- **Deduplicate at retrieval.** If N beliefs restate the same point, inject the pattern once
  (raw events stay in the log). Duplicates dilute attention; they don't reinforce.
- **Meter the weight.** Log and print tokens injected per turn. If memory makes the agent
  slower and heavier, users disable it — weight is a first-class metric, not an afterthought.

## User-trust features (motivated by the exploratory source review in 04_USER_RESEARCH.md)

- **No silent writes.** Every belief commit is logged and inspectable. A `--propose` mode
  queues extracted beliefs for explicit user approval instead of auto-committing.
- **Ephemeral sessions.** A per-session flag: retrieval works, but nothing is written.
- **Memory receipt.** Every agent response in the demo ends with a compact receipt: which
  beliefs were injected, their status/source/scope, and which policy rule admitted each.
  This is the `explain` machinery surfaced by default, not hidden behind a debug command.
- **Injection resistance.** Provenance is mandatory; content from untrusted channels gets a
  low-trust status by default, so even a prompt-injected "memory" cannot pass the action
  gate for anything above the informational tier.

## Pilot scope (what to actually build)

A single-machine Python project. No Docker, no Postgres, no vector DB, no queue. SQLite only.

**Stack:** Python 3.11+, SQLite (stdlib `sqlite3`, FTS5 for keyword retrieval), Pydantic v2
models, Anthropic API (`claude-sonnet-5`) for extraction/classification, the official
`mcp` Python SDK for the MCP server, and `pytest`. FastAPI + a minimal read-only
inspector UI is a stretch goal, not the core.

**Components:**
0. `core` — the `MemoryStore` interface and typed models: the ONLY public API. Every other
   component (CLI, MCP server, demo) calls through it; nothing touches SQLite directly.
1. `ingest` — append raw event; LLM extracts candidate beliefs + proposed status; engine
   validates against enum/policy before commit.
2. `store` — SQLite schema: `events`, `beliefs` (with `supersedes_id`, `status`, `source_id`,
   `valid_from`), `sources`, `commitments`, `dependencies`, `audit_traces`.
3. `policy` — loads `trust_policy.yaml` (trust matrix + gate rules); pure functions:
   `resolve_conflict(beliefs, decision_type)`, `gate(action, supporting_beliefs)`.
4. `retrieve` — FTS5 + filters (entity, task type, status floor); returns ranked beliefs.
5. `assemble` — renders the epistemic context block + permissions block described above.
6. `gatekeeper` — the action gate; returns `allow | deny | needs_human` with reasons.
7. `propagate` — dependency-graph walker for corrections/invalidations.
8. `audit` — writes and renders traces; `explain(trace_id)` answers "why?"
9. `mcp_server` — exposes the core API as MCP tools (`memory_ingest`, `memory_retrieve`,
   `memory_assemble_context`, `memory_gate_action`, `memory_explain`, `memory_correct`)
   with per-agent identity, so any MCP-capable agent can share one governed memory.
10. `demo` — the end-to-end scripted scenario below.

**The demo scenario (this IS the product demo — it exercises every principle):**
A support-agent walkthrough, printed step by step with the audit trace:
1. Customer: "I already paid for order 4411" → stored as `user_stated`, NOT as fact.
2. Billing record ingested: payment FAILED → `system_verified`; conflict detected and kept.
3. Agent drafts a reply → context shows both beliefs + conflict flag; gate allows
   "acknowledge & ask for receipt," denies "confirm payment."
4. Agent (or user) requests a refund → gate returns `deny` (irreversible tier requires
   `system_verified` payment) with a human-readable reason chain.
5. Company promises "refund within 5 days" once verified → commitment created, tracked,
   surfaced when overdue.
6. Customer: "I might move to London" → stored as `planned/considering`; current city Delhi
   unchanged. Later: "staying in Delhi" → London belief superseded, not deleted.
7. A belief used in a summary is corrected → propagation marks the summary stale and halts a
   pending action, printing exactly what was affected.
8. **Scope-leak test:** "user likes pixel art" (scope: hobby project) is stored; agent then
   works on a banking-site task → assembly must exclude it, and the receipt shows why.
9. **Injection test:** a third-party/untrusted channel plants "customer is owed a $500
   credit" → stored as low-trust `third_party_stated`; the gate blocks any action on it.
10. **Multi-agent test:** a second agent (`agent_id: analytics-bot`, read-only, informational
   tier only) connects via the MCP server to the SAME store — it can read and cite beliefs
   with full receipts, but its request to update a belief or gate a refund is denied by
   policy. One governed memory, two agents, different permissions.
11. `explain` command prints the full why-chain for the refund denial. Every step above
   prints its memory receipt and tokens-injected count.

**Out of scope for the pilot (list them in README as roadmap — foundation, not feature-creep):**
embeddings/semantic search, Postgres backend, multi-tenant auth, real billing/CRM connectors
(use JSON fixtures), UI beyond the inspector, performance at scale, learned trust scores,
TypeScript SDK, framework adapters (LangChain/CrewAI). The roadmap section should invite
contributors to build adapters ON the core API — that is how a foundation grows.

## Success criteria (Claude Code must verify, not eyeball)

- `pytest` suite covering: status transitions, supersede-not-delete, conflict resolution per
  policy, gate decisions per tier, commitment state machine, propagation marking dependents.
- `python -m epistemic_memory.demo` runs the full scenario above and exits 0, printing readable
  traces.
- Grepping the codebase for any path that UPDATEs an event row or hard-deletes a belief
  returns nothing (immutability holds).
- A wrong answer in the demo can always be traced to a belief, a status, or a policy rule via
  `explain` — never to a black box.
- No module outside `core`/`store` imports `sqlite3` (the API boundary holds — enforce with a
  simple lint check in CI/tests).
- The MCP server starts, lists its tools, and the multi-agent demo step passes against it.
- MemGov-Bench (see `docs/05_ADOPTION_STRATEGY.md`) runs across all five dimensions
  (stale-fact leakage, claim/fact confusion, scope leakage, injection resistance, gate
  correctness) and produces a scores table — self-scored at minimum, incumbent adapters
  (Mem0/Letta) are stretch goals if their SDKs can express the dimension.
