# EXPLORATORY SOURCE REVIEW — Memory risks and design hypotheses

This note summarizes a small, non-systematic set of essays, issue reports, product analyses,
and research papers. It is not primary user research and does not establish how prevalent any
reported problem is. The observations motivated design hypotheses; this repository's tests
validate only Epistemic Memory's implementation behavior.

## Observation 1: stale context can be difficult to diagnose

One personal essay describes turning off ChatGPT memory after outdated context accumulated.
Separately, the MemStrata paper reports stale-fact leakage and weak contradiction/restatement
separation in its own finite experiments. Those results should not be generalized to every
memory product.

Design response: beliefs have append-only versions, explicit status, structural currentness,
and same-source supersession. Cross-source disagreements remain visible conflicts.

## Observation 2: relevant content still needs a scope boundary

A useful stored preference may be inappropriate in an unrelated task even when retrieval ranks
it highly. This is a design risk rather than a measured prevalence claim.

Design response: every belief has a scope, agents have explicit readable scopes, and complete
serialization tests prove that hidden-scope content does not leak into assembled context.

## Observation 3: visibility is not the same as influence tracing

Perspectives on long-term AI memory argue for transparent, legible, contestable behavior and
meaningful user control. These are normative design arguments, not evidence that a particular
interface resolves user trust.

Design response: durable answer, action, and mutation paths create immutable audit traces.
Historical explanation uses decision-time evidence and policy snapshots, while current
follow-up is reported separately.

## Observation 4: automatic writes and duplicates can reduce confidence

A single production-user issue reports duplicate memories diluting useful context and increasing
memory weight. Consent-focused commentary also questions silent default-on memory writes.

Design response: proposal mode separates extraction from approval, and ephemeral mode opens an
existing store read-only. Retrieval deduplication is deterministic and structural: it collapses
same-source, same-key, normalized-value repetitions while retaining raw events and conflicts. It
is not semantic-similarity deduplication.

## Observation 5: contradiction handling needs deterministic structure

The MemStrata experiments use 98 contradiction/restatement pairs and report AUROC 0.59 for the
tested embedding-similarity classification. The paper also reports 15–40% stale-fact leakage in
its four evolving-memory benchmarks. These are results of that experimental setup, not universal
rates for named systems.

Design response: the engine uses `(entity, attribute)` plus exact provenance to distinguish a
same-source update from cross-source disagreement. Embeddings are not used for supersession.

## Observation 6: memory writes are a prompt-injection target

A published proof of concept demonstrates prompt content causing persistent memory writes in a
consumer assistant. It does not test this repository.

Design response: trusted hosts control exact provenance principals; authorization occurs before
extraction; source-type status ceilings clamp candidates; and retrieval never grants action
permission. The deterministic demo includes an untrusted-source denial.

## Sources

- [Personal account of context rot](https://every.to/also-true-for-humans/why-i-turned-off-chatgpt-s-memory)
- [Single production-user duplicate and weight report](https://github.com/MemPalace/mempalace/issues/514)
- [MemStrata evolving-memory experiments](https://arxiv.org/html/2606.26511)
- [Persistent-memory prompt-injection proof of concept](https://embracethered.com/blog/posts/2024/chatgpt-hacking-memories/)
- [Perspective on transparency and consent for AI memory](https://www.techpolicy.press/what-we-risk-when-ai-systems-remember/)
- [Transparent, legible, and contestable memory design principles](https://arxiv.org/html/2512.06616)
