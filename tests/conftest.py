from pathlib import Path
from types import SimpleNamespace

import pytest

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import Belief, EpistemicStatus, Source
from epistemic_memory.policy import load_policy

POLICY_PATH = str(Path(__file__).resolve().parent.parent / "trust_policy.yaml")


@pytest.fixture
def m4_memory(tmp_path):
    policy = load_policy(POLICY_PATH)
    db_path = str(tmp_path / "m4.db")
    memory = MemoryStore(db_path, policy, agent_id="support-agent")
    for source_id, source_type, label in [
        ("user", "user", "Customer chat"),
        ("billing", "billing_system", "Billing system A"),
        ("billing2", "billing_system", "Billing system B"),
        ("manager", "manager", "Support manager"),
        ("third", "third_party", "Third party"),
        ("untrusted", "untrusted_channel", "Untrusted channel"),
    ]:
        memory._store.add_source(Source(
            id=source_id,
            type=source_type,
            label=label,
            created_at="2026-07-01T00:00:00+00:00",
        ))

    def make(**overrides):
        values = {
            "entity": "order_4411",
            "attribute": "payment_status",
            "value": "paid",
            "status": EpistemicStatus.user_stated,
            "scope": "global",
            "source_id": "user",
            "decision_type": "payment_status",
            "valid_from": "2026-07-01T00:00:00+00:00",
            "created_at": "2026-07-01T00:00:00+00:00",
        }
        values.update(overrides)
        return Belief(**values)

    def add(**overrides):
        return memory._store.add_belief(make(**overrides))

    harness = SimpleNamespace(
        memory=memory,
        policy=policy,
        db_path=db_path,
        make=make,
        add=add,
    )
    yield harness
    memory.close()
