"""Create or validate the least-privileged example database."""

from __future__ import annotations

import argparse
from pathlib import Path

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import Source
from epistemic_memory.policy import load_policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    args = parser.parse_args()

    policy = load_policy(str(args.policy))
    if policy.source_principals.get("user") != "user":
        raise ValueError("example bootstrap requires the exact 'user' principal")
    memory = MemoryStore(
        str(args.db),
        policy,
        agent_id="support-agent",
        trusted_sources=[
            Source(
                id="user",
                type="user",
                label="User-provided statements",
                created_at="2026-07-12T00:00:00+00:00",
            )
        ],
    )
    memory.close()
    print(f"initialized example store: {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
