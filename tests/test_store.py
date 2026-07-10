"""M1 verification: append-only enforcement + supersede/current-belief behavior.

See PLAN.md M1 and hard constraints in 02_SPEC.md: events are append-only,
beliefs are never hard-deleted or silently edited, and no module outside
core/store may import sqlite3.
"""

import ast
import sqlite3
from pathlib import Path

import pytest

from epistemic_memory.models import Belief, Event, EpistemicStatus, Source
from epistemic_memory.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    s.add_source(Source(id="user", type="user", label="Customer chat", created_at="2026-07-01"))
    s.add_source(
        Source(id="billing", type="billing_system", label="Billing system", created_at="2026-07-01")
    )
    yield s
    s.close()


def make_belief(**overrides) -> Belief:
    defaults = dict(
        entity="order_4411",
        attribute="payment_status",
        value="paid",
        status=EpistemicStatus.user_stated,
        scope="global",
        source_id="user",
        valid_from="2026-07-01",
        created_at="2026-07-01",
    )
    defaults.update(overrides)
    return Belief(**defaults)


# --------------------------- immutability: events ---------------------------


def test_event_update_raises(store):
    ev = store.add_event(
        Event(source_id="user", content="hello", scope="global", created_at="2026-07-01")
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE events SET content = 'edited' WHERE id = ?", (ev.id,))


def test_event_delete_raises(store):
    ev = store.add_event(
        Event(source_id="user", content="hello", scope="global", created_at="2026-07-01")
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events WHERE id = ?", (ev.id,))


# -------------------------- immutability: beliefs ---------------------------


def test_belief_update_raises(store):
    b = store.add_belief(make_belief())
    with pytest.raises(sqlite3.IntegrityError, match="versioned"):
        store.conn.execute("UPDATE beliefs SET value = 'FAILED' WHERE id = ?", (b.id,))


def test_belief_delete_raises(store):
    b = store.add_belief(make_belief())
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.conn.execute("DELETE FROM beliefs WHERE id = ?", (b.id,))


# ------------------------------- supersede ----------------------------------


def test_supersede_creates_new_version_old_stays_queryable(store):
    london = store.add_belief(
        make_belief(
            entity="customer_881",
            attribute="planned_city",
            value="London",
            status=EpistemicStatus.considering,
            valid_from="2026-06-01",
            created_at="2026-06-01",
        )
    )
    assert store.is_current(london.id)

    stayed = store.supersede(
        london.id,
        make_belief(
            entity="customer_881",
            attribute="planned_city",
            value="none",
            status=EpistemicStatus.retracted,
            valid_from="2026-06-15",
            created_at="2026-06-15",
        ),
    )

    # old version untouched and still readable (never deleted)
    reloaded_london = store.get_belief(london.id)
    assert reloaded_london is not None
    assert reloaded_london.value == "London"
    assert reloaded_london.status == EpistemicStatus.considering

    # old version is no longer current; new version is
    assert not store.is_current(london.id)
    assert store.is_current(stayed.id)
    assert store.valid_to(london.id) == "2026-06-15"

    # full history is walkable oldest -> newest
    chain = store.belief_chain(stayed.id)
    assert [b.value for b in chain] == ["London", "none"]


def test_supersede_key_mismatch_rejected(store):
    b = store.add_belief(make_belief())
    with pytest.raises(ValueError):
        store.supersede(b.id, make_belief(entity="order_9999"))


def test_cross_source_supersession_rejected_so_disagreement_must_coexist(store):
    user_claim = store.add_belief(make_belief())
    with pytest.raises(ValueError, match="same-source only"):
        store.supersede(
            user_claim.id,
            make_belief(
                value="FAILED",
                source_id="billing",
                status=EpistemicStatus.system_verified,
            ),
        )
    assert store.is_current(user_claim.id)


def test_already_superseded_belief_cannot_branch(store):
    original = store.add_belief(make_belief(value="first"))
    store.supersede(original.id, make_belief(value="second"))
    with pytest.raises(ValueError, match="already superseded"):
        store.supersede(original.id, make_belief(value="third"))
    current = store.current_beliefs(original.entity, original.attribute)
    assert [belief.value for belief in current] == ["second"]


def test_belief_event_provenance_must_match_source(store):
    event = store.add_event(
        Event(source_id="user", content="I paid", scope="global", created_at="2026-07-01")
    )
    with pytest.raises(ValueError, match="does not match event source"):
        store.add_belief(make_belief(source_id="billing", event_id=event.id))


def test_nonexistent_belief_is_not_current(store):
    assert store.is_current(999_999) is False


# ---------------------------- current_beliefs -------------------------------


def test_current_beliefs_excludes_superseded(store):
    v1 = store.add_belief(
        make_belief(entity="customer_881", attribute="current_city", value="Delhi")
    )
    store.supersede(
        v1.id,
        make_belief(entity="customer_881", attribute="current_city", value="Mumbai"),
    )
    current = store.current_beliefs("customer_881", "current_city")
    assert [b.value for b in current] == ["Mumbai"]


def test_current_beliefs_keeps_cross_source_conflicts_both_live(store):
    """D2: conflict (different sources) leaves both current; only same-lineage
    supersession removes a belief from current_beliefs()."""
    store.add_belief(
        make_belief(
            entity="order_4411",
            attribute="payment_status",
            value="paid",
            status=EpistemicStatus.user_stated,
            source_id="user",
        )
    )
    store.add_belief(
        make_belief(
            entity="order_4411",
            attribute="payment_status",
            value="FAILED",
            status=EpistemicStatus.system_verified,
            source_id="billing",
        )
    )
    current = store.current_beliefs("order_4411", "payment_status")
    assert {b.value for b in current} == {"paid", "FAILED"}


def test_current_beliefs_ties_use_immutable_id_order(store):
    first = store.add_belief(make_belief(value="first", valid_from="2026-07-01"))
    second = store.add_belief(
        make_belief(
            value="second", source_id="billing", status=EpistemicStatus.system_verified,
            valid_from="2026-07-01",
        )
    )
    current = store.current_beliefs("order_4411", "payment_status")
    assert [belief.id for belief in current] == [first.id, second.id]


# --------------------------- API boundary (sqlite3) --------------------------


def test_no_module_outside_core_store_imports_sqlite3():
    allowed = {REPO_ROOT / "epistemic_memory" / "store.py"}
    offenders = []
    for path in (REPO_ROOT / "epistemic_memory").rglob("*.py"):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == "sqlite3" for a in node.names):
                offenders.append(str(path))
            if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                offenders.append(str(path))
    assert not offenders, f"sqlite3 imported outside store.py: {offenders}"
