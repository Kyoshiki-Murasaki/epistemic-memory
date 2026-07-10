# USER RESEARCH — What real users say is broken in AI memory (and how this spec answers it)

Honesty note: direct Reddit thread retrieval wasn't available at research time, so this file
draws on sources that document and aggregate the same community complaints — user-experience
essays, a production-feedback GitHub issue filed by a real user, product analyses of ChatGPT
memory backlash, and 2026 technical critiques of Mem0/Zep/Letta/Cognee. The pattern across
all of them is consistent. Each pain point below maps to a requirement in `02_SPEC.md`.
Give this file to Claude Code alongside the other docs — it explains WHO the features are for.

## Pain point 1: "Context rot" — stale and wrong memories quietly degrade answers
Users report the slow buildup of stale preferences, errors, and contradictions that silently
corrupts results: the assistant remembering the wrong employer months after a job change,
recommending tools the user explicitly said they stopped using, or over-applying an old
throwaway remark to every future conversation. Some power users turn memory OFF entirely
because they can no longer predict or diagnose how past chats bias current answers.
→ Spec answer: epistemic status + `valid_from/valid_to` windows + supersession instead of
  silent overwrite + staleness checks in the action gate. (Principles 1, 2, 4)

## Pain point 2: Right memory, wrong context — personalization leaks everywhere
The most-cited annoyance: a preference stated once gets applied indiscriminately — a style
preference bleeding into debugging help, location data making unrelated answers "creepy."
Users want memory that is relevant to the current task, not a personality tattoo.
→ NEW spec addition: every belief gets a **scope** (`global | project:<id> | persona | task_type`)
  and context assembly filters by scope + task relevance. The demo now includes an
  over-application test: a stored "user likes pixel art" belief must NOT be injected into a
  banking-website task. (This was §5 of the vision doc; user research confirms it's a top-3
  complaint, so it's promoted from principle to tested feature.)

## Pain point 3: Memory is a black box — no influence tracing
Users consistently ask for three things: transparency (see exactly what is remembered),
control (edit/delete/veto), and focus. The strongest formulation: not just visibility but
*influence tracing* — show not only what is stored but how each memory shaped this specific
answer, like a receipt. Products that display "what was recalled, from where, and why" are
called out as the trust benchmark. Research frames it as visibility + interpretability +
contestability.
→ Spec answer: the audit trace + `explain` command. NEW addition: `explain` output is
  user-facing by design — every demo response prints a compact "memory receipt" (which
  beliefs were injected, status, source, and which rule allowed each).

## Pain point 4: Silent writes — the system remembers things you never approved
Memory being saved automatically, without consent or notice, is described as the moment
trust breaks ("without an explicit veto model trust breaks"). Deleting a chat not deleting
its memories makes it worse.
→ NEW spec addition: no silent commits. Every belief write is logged and inspectable; a
  `--propose` mode queues extracted beliefs for user approval instead of auto-committing;
  an **ephemeral session** flag runs a conversation with retrieval-only (no writes).

## Pain point 5: Duplicates dilute; memory gets "too heavy" and slows everything down
From a real production-feedback issue: 12 stored memories where 10 restated the same
principle — "the duplicates didn't reinforce, they diluted"; and the killer observation that
if memory makes the AI better-informed but slower, users eventually disable it.
→ NEW spec addition: retrieval-time semantic deduplication (surface a repeated pattern once,
  keep raw events in the log), and the demo prints **tokens injected per turn** so memory
  weight is measured, not felt.

## Pain point 6: Contradiction handling by similarity is provably broken
A 2026 result devs cite: embedding similarity cannot distinguish "contradicts a stored fact"
from "restates a stored fact" (near-chance AUROC), so LLM/similarity-based conflict
resolution leaks stale facts; supersession must be deterministic and structural. Mem0 is
specifically criticized for having no structural supersession, no validity windows, and no
version history — "what did the user prefer last quarter" is unanswerable.
→ NEW spec addition: beliefs carry a structural key `(entity, attribute)`; contradiction
  detection is a deterministic key match, not an embedding threshold. The LLM extracts the
  key; the engine does the superseding. Version history is already core (supersede chains).

## Pain point 7: Memory injection — attackers can write memories via prompt injection
Documented attacks show hostile content can silently create false memories that persist
across sessions, and models sometimes hallucinate memories that were never stored.
→ Spec answer (strengthened): provenance is mandatory; content originating from untrusted
  channels defaults to low-trust status; the action gate means even a successfully injected
  "memory" cannot authorize a high-stakes action because its status/source fails the policy.
  Add one demo step: an injected `third_party_stated` belief tries and fails to pass the gate.

## Pain point 8: No definitive solution — every tool wins one axis and loses another
The community's meta-complaint: Mem0 is easy but shallow on time and audit; Zep handles time
but the platform pulled back from open self-hosting; Letta is powerful but you adopt a whole
runtime; benchmark scores are disputed between vendors. Reviewers explicitly note the 2026
benchmark leader "has no audit model," and regulatory pressure (EU AI Act) is pushing audit
trails toward being default expectations.
→ Positioning answer: this project doesn't compete on recall benchmarks at all. It occupies
  the governance axis every incumbent is weak on: status, trust policy, gating, propagation,
  audit. Say this explicitly in the README — it converts "yet another memory repo" into a
  distinct category.

## Sources (for the README's references section)
- every.to/also-true-for-humans/why-i-turned-off-chatgpt-s-memory — "context rot," over-application examples
- aitoolbriefing.com/industry/chatgpt-memory-dreaming-v3-overhaul-2026 — staleness complaints OpenAI acknowledged
- github.com/MemPalace/mempalace/issues/514 — production user feedback: duplicate dilution, memory weight
- arxiv.org/html/2606.26511 (MemStrata) — similarity-based contradiction detection is structurally broken
- dev.to/jonathanfarrow/the-10-best-ai-memory-layers-for-agents-in-2026-448e — per-tool trade-off critiques
- joshuaberkowitz.us (Le Chat memory analysis) — transparency/control/focus, memory "receipts"
- embracethered.com (ChatGPT memory deep dive) — memory injection, hallucinated memories
- innobu.com agent-memory comparison — "no audit model," veto/trust, half-life of truth
- techpolicy.press/what-we-risk-when-ai-systems-remember — consent, silent default-on memory
- arxiv.org/pdf/2512.06616 — visibility / interpretability / contestability framework
