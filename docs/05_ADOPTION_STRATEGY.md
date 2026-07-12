# ADOPTION AND BENCHMARK NOTES

Epistemic Memory is a local reference implementation and finite deterministic conformance
harness. The current release implements only the bundled `ours` benchmark adapter. It reports no
result for Mem0, Zep, Letta, or any other external system, and it makes no performance,
production-readiness, or statistical-generalization claim.

## MemGov-Bench

MemGov-Bench evaluates five governance dimensions:

1. stale-fact leakage;
2. claim/fact confusion;
3. scope leakage;
4. injection resistance; and
5. gate correctness.

The canonical M11 suite contains ten original synthetic cases, two per dimension. Run it with:

```bash
python -m memgov_bench --adapter ours
```

The reference adapter calls only public `MemoryStore` APIs. Static expectations are independent
of adapter output, and each case uses fresh temporary state, fixed UTC time, and deterministic
IDs. A case passes only when its complete typed observation matches its frozen expectation. A
dimension passes only when both cases pass, and the overall result passes only when all five
dimensions pass in each of exactly three complete runs. Harness errors are reported separately
from valid case failures.

The accepted deterministic output records 10/10 cases in all three internal runs, 100.00% mean,
minimum, and maximum correctness, and 0.0000 percentage-points-squared observed correctness
variance. Its SHA-256 is:

```text
ebacd6df735e81a51585500873b2703576d45f19d39c3321017959752db4884f
```

This result establishes conformance only for the bundled implementation on the checked-in finite
suite. External adapters, live-model trials, timing measurements, and independently maintained
expectations would be separate future work.

## Local evaluation path

The repository is not published as a package. Evaluate the checked-out source locally:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m epistemic_memory.demo
.venv/bin/python -m memgov_bench --adapter ours
```

The demo and benchmark require no API key, Docker service, or external database. Live Anthropic
extraction is an explicit opt-in path and is not used by either deterministic artifact.

## Publication and maintenance boundaries

Repository publication should keep the README, demo hash, benchmark hash, supported commands,
and limitations synchronized with tests. New benchmark dimensions or external adapters should
ship only with independent fixtures and explicit scope statements; no external system should be
scored without a maintained adapter and reproducible evidence.

No licence is declared. Do not assume reuse rights that the repository has not granted. There is
no hosted service, remote MCP transport, package release, support promise, or maintenance SLA.
