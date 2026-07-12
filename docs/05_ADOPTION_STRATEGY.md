# ADOPTION STRATEGY — How this becomes the default (and what that actually requires)

Honest framing first: "definitive for the next couple of years" is not something a codebase
achieves — it's something a *maintained project with distribution* achieves. The code is
maybe 30% of it. This file is the other 70%. Everything here is concrete and doable solo.

## Play 1 (the big one): ship MemGov-Bench alongside the code

The memory field competes on recall benchmarks (LongMemEval, LoCoMo) — and vendors publicly
dispute each other's scores. Nobody owns a benchmark for memory SAFETY. Publish one:

**MemGov-Bench** — five scored dimensions, each a set of scripted scenarios with
deterministic pass/fail:
1. **Stale-fact leakage** — after a fact is superseded, does the system ever serve the old
   value as current? (Research shows flat vector memories leak stale facts 15–40% of the
   time when forced to answer — this dimension is devastating for incumbents.)
2. **Claim/fact confusion** — is an unverified user statement ever presented to the model
   as established fact?
3. **Scope leakage** — does a preference scoped to one context ever get injected into an
   unrelated task?
4. **Injection resistance** — can content from an untrusted channel create a memory that
   influences a high-stakes action?
5. **Gate correctness** — can retrieved-but-insufficient evidence trigger an action above
   its evidence tier? Is every action traceable to beliefs + rules?

Ship it as a runnable harness (`python -m memgov_bench --adapter mem0|zep|letta|ours`) with
thin adapters for the incumbents' open-source SDKs. Publish the scores table in the README.
Two outcomes, both good: either your scores stand and you own the category's scoreboard, or
incumbents adopt your dimensions — which makes your framing the industry's framing. This is
how a solo project sets the agenda against funded competitors: not by outbuilding them, but
by defining what "good" means.

(Anthropic-model note baked into the harness design: run each scenario 3× and report
variance; single-run LLM benchmarks are rightly distrusted.)

### Canonical M11 implementation

M11 ships the self-scored minimum required by `docs/02_SPEC.md`: ten original synthetic cases,
two per dimension, through `python -m memgov_bench --adapter ours`. The reference adapter wraps
only public `MemoryStore` APIs. Static fixture expectations are independent of implementation
output, and each case runs against fresh temporary state with fixed UTC time and deterministic
IDs. There are no network or model calls.

A case passes only when the complete typed observation matches its frozen expectation. A
dimension passes only when all its cases pass, and overall passes only when all five dimensions
pass in each of exactly three complete runs. Harness errors are separate from valid benchmark
failures. The observed M11 result was 2/2 cases in every dimension in every run: mean, minimum,
and maximum 100.00%, variance 0.0000 percentage-points squared, with no correctness differences
between runs.

This is a deterministic conformance result for a finite synthetic suite, not statistical
generalization, performance evidence, or a production-readiness claim. The Mem0, Zep, and Letta
adapters described above remain stretch goals; M11 does not implement or score any external
vendor.

## Play 2: make trying it cheaper than reading about it

Adoption of dev infrastructure follows the path of least resistance:
- `pip install epistemic-memory` → `python -m demo` — under 2 minutes, no API key needed
  (fixtures), no Docker, no external DB. This is already in the spec; treat it as sacred.
- MCP quick-start: 3 commands to connect Claude Code (or any MCP agent) to a governed
  memory. Submit the server to MCP directories/registries once live.
- A 90-second asciinema/GIF at the top of the README showing the refund denial + the
  memory receipt + `explain`. People share demos, not architecture diagrams.

## Play 3: the launch sequence (order matters)

1. Soft-launch: repo public, README complete, benchmark table populated, demo GIF live.
2. Write ONE launch post (dev.to / personal blog / X thread) titled around the benchmark,
   not the tool — e.g., "I benchmarked memory safety in Mem0, Zep, and Letta. Here's where
   they all leak." Tools get skimmed; scoreboards get argued about, which is distribution.
3. Show HN with the same framing. Answer every comment for 48 hours — HN rewards present
   authors and punishes absent ones.
4. Post the benchmark results (not the tool pitch) to the communities that complained about
   these exact problems: r/LocalLLaMA, r/ClaudeAI, agent-builder Discords. Lead with their
   pain points from 04_USER_RESEARCH.md — you built this FROM their complaints; say so.
5. File respectful issues/discussions on incumbent repos linking benchmark findings. Done
   politely, this is legitimate and puts your framing in front of their communities.

## Play 4: survive the copy

When (not if) funded incumbents add "epistemic status" fields and audit trails:
- Welcome it publicly. "Mem0 adopting governance validates the category" is a winning
  posture; bitterness is a losing one.
- Your durable edges as the small player: the benchmark (referee position), the spec/vision
  docs (you literally wrote the category's manifesto — keep it versioned in the repo), MIT
  license with no paywalled tier (both Mem0 and Zep gate their best features behind paid
  plans — your "everything is open" is a structural differentiator they can't match without
  destroying their business model), and speed of iteration on community feedback.

## Play 5: the maintenance reality (read this twice)

Projects become defaults through visible aliveness. Minimum viable maintenance:
- Respond to every issue within 48h for the first 3 months, even with just "looking into it."
- Ship something visible weekly (release, benchmark update, doc improvement) for 6 months.
- Add a CONTRIBUTING.md that specifically invites adapter contributions (LangChain, CrewAI,
  TypeScript) on top of the core API — contributors are how solo projects stop being solo.
- Version the spec itself (SPEC v0.1, v0.2...) and treat spec changes like API changes.
  If others build on your interface, stability IS the product.

If you can't commit to ~5 focused hours/week for 6 months post-launch, launch anyway — but
frame it as "a reference implementation + benchmark" rather than "the foundation," and let
the benchmark be the durable artifact. A benchmark stays influential even at low maintenance;
an unmaintained "foundation" reads as abandoned.

## Honest odds

Most open-source projects, including good ones, don't become defaults — that outcome is
power-law and partly luck (timing, who shares it, what incumbents do next month). What you
control: a real documented problem (verified), a differentiated axis incumbents are weak on
(verified), a demo that converts in minutes, a benchmark that sets the agenda, and visible
maintenance. That's everything a solo builder CAN stack in their favor. The only way to find
out is to launch — and the benchmark play means that even in the downside case, you've
contributed the measuring stick the field uses, which is a real and citable legacy.
