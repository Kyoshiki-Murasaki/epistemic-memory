# The exact prompt to paste into Claude Code

Setup first (5 minutes):
```bash
mkdir epistemic-memory && cd epistemic-memory
git init
mkdir docs
# copy AI_Memory_System.markdown and 02_SPEC.md into docs/
claude
```
Then paste everything below the line as your first message.

---

You are building the pilot of **Epistemic Memory** — an open-source memory *foundation* for
AI: a common, governed memory layer that many agents and tools can share, which knows where
information came from, how certain it is, where it may be used, whether it is current, what
disagrees with it, and whether it is strong enough to act on. This is a greenfield project.
Three documents in `docs/` define it:

- `docs/AI_Memory_System.markdown` — the vision: why this is different from Mem0/Zep/Letta.
- `docs/02_SPEC.md` — the binding technical spec: architecture, components, stack, demo
  scenario, and success criteria.
- `docs/04_USER_RESEARCH.md` — real-user pain points with existing memory tools; explains
  who each feature is for. Use it for README framing and to sanity-check design decisions.
- `docs/05_ADOPTION_STRATEGY.md` — the launch and benchmark strategy; relevant to M10–M11
  (README framing and MemGov-Bench design).

Read ALL of them fully before writing any code. The spec is authoritative; where the vision doc is
looser, the spec decides. If the two conflict or something is ambiguous in a way that changes
what you'd build, STOP and ask me — do not guess silently.

## Your working rules for this whole project

1. **Think before coding.** Start by writing `PLAN.md`: milestones, each with a concrete
   verification step ("→ verify: pytest tests/test_gate.py passes"). Show me the plan and the
   proposed SQLite schema + Pydantic models BEFORE implementing anything. Wait for my OK.
2. **Simplicity first.** Minimum code that satisfies the spec. No speculative abstractions, no
   plugin systems, no config options nobody asked for, no error handling for impossible cases.
   SQLite + stdlib + the few listed libraries only. If a component took 200 lines and could
   take 50, rewrite it.
3. **Surgical changes.** In later sessions, touch only what the task requires. Don't reformat
   or "improve" adjacent code. Every changed line must trace back to the request.
4. **Goal-driven.** Every milestone ends with its verification actually run and passing. The
   final gate is the spec's "Success criteria" section — run all of it, show me the output.

## Milestones (put these in PLAN.md, refine as needed)

- M0: repo scaffold, pyproject, README stub, `trust_policy.yaml` with the demo's rules.
- M1: `core` — the `MemoryStore` interface + Pydantic models. This is the project's ONLY
  public API; design it first and keep it small. Then store: schema, append-only event log,
  versioned beliefs with `supersedes`. Tests prove immutability (no UPDATE on events, no
  DELETE on beliefs) and that no module outside core/store imports sqlite3.
- M2: ingest — LLM extraction of candidate beliefs with proposed epistemic status; engine
  validates status against the closed enum and policy before commit. Use fixtures so tests
  run without API calls; live API behind a flag.
- M3: policy engine — trust matrix conflict resolution + action gate with the four risk
  tiers + per-agent permissions (`agent_id` scoping reads and allowed action tiers). Pure
  functions, exhaustively unit-tested.
- M4: retrieval (FTS5 + filters incl. scope) and context assembly — epistemic headers,
  conflict flags, permissions block, retrieval-time dedup, token budget + per-turn token
  metering, and the memory receipt. This is the heart; snapshot-test the rendered blocks.
  Structural note: contradictions are detected by deterministic (entity, attribute) key
  match at ingest — never by embedding similarity.
- M5: commitments table + state machine; overdue surfacing.
- M6: dependency graph + correction propagation (mark stale, halt pending, report executed).
- M7: audit traces + `explain` command + user-trust modes (`--propose` approval queue,
  ephemeral no-write sessions; no silent belief commits anywhere).
- M8: MCP server — expose the core API as MCP tools with per-agent identity, using the
  official `mcp` Python SDK. Verify: server starts, tools list, and a scripted second agent
  with read-only/informational permissions is correctly limited by policy.
- M9: `python -m demo` — the full scripted support scenario from the spec (all 11 steps,
  including the scope-leak, injection, and multi-agent tests), with rich, readable
  step-by-step output, memory receipts, and token counts. The showpiece; make it beautiful.
- M10: README for GitHub — open with the mission statement from the spec verbatim, then the
  pitch (memory that knows what it's allowed to trust), a comparison table vs Mem0/Zep/Letta
  on the governance axes (epistemic status, trust policy, action gating, propagation,
  audit, multi-agent permissions), MCP quick-start (connect Claude Code to it in 3 steps),
  the demo GIF/asciinema instructions, and a roadmap that invites contributors to build
  adapters on the core API.
- M11 (the category play — see docs/05_ADOPTION_STRATEGY.md): **MemGov-Bench**, a runnable
  memory-safety benchmark with five dimensions: stale-fact leakage, claim/fact confusion,
  scope leakage, injection resistance, gate correctness. Deterministic scripted scenarios,
  pass/fail scoring, run 3× with variance reported. Ship with an adapter interface and a
  reference adapter for our own system; adapters for Mem0/Letta open-source SDKs as stretch
  (stub them cleanly if their APIs can't express a dimension — "cannot express" is itself a
  reportable result). Output: a markdown scores table the README embeds.

## Hard constraints

- Events are append-only. Beliefs are never hard-deleted or silently edited — only
  superseded/retracted via new versions. Enforce at the data-access layer, verify in tests.
- The LLM never commits a status change; it only proposes. The engine + policy commit.
- Retrieval never implies permission. Every action path goes through the gate.
- All access goes through the `MemoryStore` core API with an `agent_id`. No client — CLI,
  MCP server, or demo — touches SQLite directly.
- No cloud services, no Docker, no external DB. `pip install -e .` then `python -m demo`
  must work on a clean machine (with ANTHROPIC_API_KEY optional thanks to fixtures).

Start now with M0 + PLAN.md + proposed schema, then pause for my review.

---

## Follow-up prompts you'll want later (keep for reference)

- After each milestone: "Run the milestone's verification, paste the output, then a 5-line
  summary of what changed and why. Do not start the next milestone yet."
- If it over-builds: "This violates simplicity-first. Rewrite with the minimum that passes
  the tests."
- Before publishing: "Audit the repo against docs/02_SPEC.md success criteria one by one and
  produce a compliance report. Fix any gaps, smallest change possible."
- For the README polish: "Write the README for a skeptical senior engineer who already uses
  Mem0. Lead with the refund demo transcript, not with adjectives."
