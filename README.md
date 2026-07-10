# Epistemic Memory

A trust-and-governance memory layer for AI agents.

Existing memory systems (Mem0, Zep, Letta) answer **"what do I remember?"**
This one also answers:

> **"why should I trust it, am I allowed to use it here, and is it strong enough to act on?"**

Every stored belief carries an **epistemic status**, a **source**, a **scope**, and a
**structural key** — and no memory ever grants permission to act. A separate policy-driven
**action gate** decides, per risk tier, whether the supporting evidence is current,
uncontradicted, authorized, and strong enough.

> 🚧 **Pilot in progress.** See [PLAN.md](PLAN.md) for the milestones, the proposed schema,
> and the design decisions. The full pitch, comparison table, and demo instructions land in
> M9. Run the showpiece with `python -m demo` (M8).
