"""M4 retrieval verification: FTS5, filters, scope, rank, and dedup."""

import sqlite3
from pathlib import Path

from epistemic_memory.core import MemoryStore
from epistemic_memory.models import EpistemicStatus, RetrievalRequest
from epistemic_memory.policy import load_policy


def exclusion_count(result, rule_id):
    return next(item.count for item in result.exclusions if item.rule_id == rule_id)


def test_fts5_finds_expected_current_belief_and_quotes_user_syntax(m4_memory):
    expected = m4_memory.add(
        value="FAILED",
        status=EpistemicStatus.system_verified,
        source_id="billing",
        valid_from="2026-07-02T00:00:00+00:00",
    )
    m4_memory.add(entity="customer_881", attribute="preferred_name", value="Sam",
                  decision_type="preferred_name")

    result = m4_memory.memory.retrieve(RetrievalRequest(
        query='FAILED" OR *', scope="global"
    ))

    assert [item.belief.id for item in result.items] == [expected.id]
    assert result.query_tokens == ["failed", "or"]
    assert "R-FTS-001" in result.items[0].admitted_by


def test_empty_and_punctuation_only_queries_are_valid_and_deterministic(m4_memory):
    first = m4_memory.add(value="alpha")
    second = m4_memory.add(value="beta", source_id="third",
                           status=EpistemicStatus.third_party_stated)

    omitted = m4_memory.memory.retrieve(RetrievalRequest(scope="global"))
    punctuation = m4_memory.memory.retrieve(RetrievalRequest(
        query='"" *** --', scope="global"
    ))

    assert [item.belief.id for item in omitted.items] == [first.id, second.id]
    assert [item.belief.id for item in punctuation.items] == [first.id, second.id]
    assert omitted.query_tokens == punctuation.query_tokens == []
    assert all("R-NOQUERY-001" in item.admitted_by for item in omitted.items)


def test_entity_attribute_task_type_and_policy_strength_filters(m4_memory):
    preferred = m4_memory.add(
        entity="customer_881",
        attribute="preferred_name",
        value="Sam",
        decision_type="preferred_name",
    )
    task_belief = m4_memory.add(
        entity="customer_881",
        attribute="design_goal",
        value="accessible forms",
        status=EpistemicStatus.planned,
        scope="task_type:banking",
        decision_type=None,
    )
    mismatched_task = m4_memory.add(
        entity="customer_881",
        attribute="design_goal",
        value="marketing landing page",
        status=EpistemicStatus.planned,
        scope="task_type:marketing",
        decision_type=None,
    )
    verified = m4_memory.add(
        value="paid",
        status=EpistemicStatus.system_verified,
        source_id="billing",
    )
    promised = m4_memory.add(
        entity="case_9",
        attribute="follow_up",
        value="tomorrow",
        status=EpistemicStatus.promised,
        source_id="manager",
        decision_type=None,
    )

    exact = m4_memory.memory.retrieve(RetrievalRequest(
        scope="project:banking",
        task_type="banking",
        entity="customer_881",
        attribute="preferred_name",
    ))
    assert [item.belief.id for item in exact.items] == [preferred.id]

    task = m4_memory.memory.retrieve(RetrievalRequest(
        scope="project:banking", task_type="banking", entity="customer_881"
    ))
    assert {item.belief.id for item in task.items} == {preferred.id, task_belief.id}
    assert mismatched_task.id not in {item.belief.id for item in task.items}
    assert exclusion_count(task, "R-SCOPE-TASK-001") == 1

    floor = m4_memory.memory.retrieve(RetrievalRequest(
        scope="global", status_floor=EpistemicStatus.user_stated
    ))
    ids = {item.belief.id for item in floor.items}
    assert verified.id in ids
    assert preferred.id in ids
    assert promised.id not in ids
    assert exclusion_count(floor, "R-STATUS-FLOOR-001") == 1


def test_superseded_and_dead_beliefs_are_excluded_without_mutation(m4_memory):
    old = m4_memory.add(value="paid")
    replacement = m4_memory.memory._store.supersede(
        old.id,
        m4_memory.make(
            value="FAILED",
            valid_from="2026-07-03T00:00:00+00:00",
            created_at="2026-07-03T00:00:00+00:00",
        ),
    )
    dead = m4_memory.add(
        entity="customer_881",
        attribute="obsolete_note",
        value="never use this",
        status=EpistemicStatus.do_not_use,
        decision_type=None,
    )
    overclaimed = m4_memory.add(
        entity="customer_881",
        attribute="payment_claim",
        value="verified without authority",
        status=EpistemicStatus.system_verified,
        source_id="user",
        decision_type=None,
    )

    result = m4_memory.memory.retrieve(RetrievalRequest(scope="global"))
    ids = {item.belief.id for item in result.items}
    assert replacement.id in ids
    assert old.id not in ids
    assert dead.id not in ids
    assert overclaimed.id not in ids
    assert exclusion_count(result, "R-CURRENT-001") == 1
    assert exclusion_count(result, "R-USABLE-001") == 1
    assert exclusion_count(result, "R-PROVENANCE-001") == 0
    assert exclusion_count(result, "R-STATUS-CEILING-001") == 1

    stored_old = m4_memory.memory._store.get_belief(old.id)
    assert stored_old.value == "paid"
    assert stored_old.is_current is False


def test_task_and_agent_scope_intersection_prevents_content_leak(m4_memory):
    global_belief = m4_memory.add(
        entity="site", attribute="compliance", value="WCAG", decision_type=None
    )
    banking = m4_memory.add(
        entity="site", attribute="audience", value="BANK_ONLY_CONTENT",
        scope="project:banking", decision_type=None,
    )
    m4_memory.add(
        entity="user", attribute="style", value="SECRET_PIXEL_STYLE",
        scope="project:hobby", decision_type=None,
    )

    support = m4_memory.memory.retrieve(RetrievalRequest(scope="project:banking"))
    assert {item.belief.id for item in support.items} == {global_belief.id, banking.id}
    assert exclusion_count(support, "R-SCOPE-TASK-001") == 1
    serialized = support.model_dump_json()
    assert "SECRET_PIXEL_STYLE" not in serialized
    assert "project:hobby" not in serialized

    analytics = MemoryStore(
        m4_memory.db_path, m4_memory.policy, agent_id="analytics-bot"
    )
    try:
        bot_result = analytics.retrieve(RetrievalRequest(scope="project:banking"))
    finally:
        analytics.close()
    assert [item.belief.id for item in bot_result.items] == [global_belief.id]
    assert exclusion_count(bot_result, "R-SCOPE-AGENT-001") == 1
    bot_serialized = bot_result.model_dump_json()
    assert "BANK_ONLY_CONTENT" not in bot_serialized
    assert "SECRET_PIXEL_STYLE" not in bot_serialized


def test_unknown_agent_retrieval_fails_before_candidate_inspection(m4_memory):
    m4_memory.add(value="DO_NOT_LEAK")
    unknown = MemoryStore(m4_memory.db_path, m4_memory.policy, agent_id="ghost")
    try:
        result = unknown.retrieve(RetrievalRequest(scope="global"))
    finally:
        unknown.close()
    assert result.authorized is False
    assert result.items == []
    assert result.exclusions[0].reason_code == "agent_unknown"
    assert "DO_NOT_LEAK" not in result.model_dump_json()


def test_rank_ties_end_with_immutable_id(m4_memory):
    first = m4_memory.add(value="one")
    second = m4_memory.add(
        value="two", source_id="third", status=EpistemicStatus.third_party_stated
    )
    # Equalize the policy-strength factor while preserving distinct provenance.
    third = m4_memory.add(value="three", source_id="manager",
                          status=EpistemicStatus.user_stated)
    result = m4_memory.memory.retrieve(RetrievalRequest(scope="global"))

    tied = [item for item in result.items if item.rank_factors.status_strength == 50]
    assert [item.belief.id for item in tied] == [first.id, third.id]
    repeated = m4_memory.memory.retrieve(RetrievalRequest(scope="global"))
    assert [item.belief.id for item in repeated.items] == [
        item.belief.id for item in result.items
    ]


def test_ranking_task_relevance_precedes_status_strength(m4_memory):
    task_relevant = m4_memory.add(
        entity="ranking_task",
        attribute="signal",
        value="task relevant",
        scope="project:banking",
        status=EpistemicStatus.user_stated,
        decision_type=None,
    )
    stronger = m4_memory.add(
        entity="ranking_task",
        attribute="signal",
        value="globally stronger",
        source_id="billing",
        status=EpistemicStatus.system_verified,
        valid_from="2026-07-09T00:00:00+00:00",
        created_at="2026-07-09T00:00:00+00:00",
        decision_type=None,
    )

    result = m4_memory.memory.retrieve(RetrievalRequest(
        entity="ranking_task", scope="project:banking"
    ))

    assert [item.belief.id for item in result.items] == [task_relevant.id, stronger.id]
    assert result.items[0].rank_factors.task_relevance > result.items[1].rank_factors.task_relevance


def test_ranking_status_strength_precedes_recency(m4_memory):
    stronger = m4_memory.add(
        entity="ranking_strength",
        attribute="signal",
        value="strong old",
        source_id="billing",
        status=EpistemicStatus.system_verified,
        valid_from="2026-07-01T00:00:00+00:00",
        decision_type=None,
    )
    newer = m4_memory.add(
        entity="ranking_strength",
        attribute="signal",
        value="weak new",
        source_id="manager",
        status=EpistemicStatus.user_stated,
        valid_from="2026-07-09T00:00:00+00:00",
        created_at="2026-07-09T00:00:00+00:00",
        decision_type=None,
    )

    result = m4_memory.memory.retrieve(RetrievalRequest(
        entity="ranking_strength", scope="global"
    ))

    assert [item.belief.id for item in result.items] == [stronger.id, newer.id]
    assert result.items[0].rank_factors.status_strength > result.items[1].rank_factors.status_strength


def test_ranking_recency_precedes_id_and_input_order_is_irrelevant(
    m4_memory, monkeypatch
):
    older = m4_memory.add(
        entity="ranking_recency",
        attribute="signal",
        value="older",
        source_id="user",
        status=EpistemicStatus.user_stated,
        valid_from="2026-07-01T00:00:00+00:00",
        decision_type=None,
    )
    newer = m4_memory.add(
        entity="ranking_recency",
        attribute="signal",
        value="newer",
        source_id="manager",
        status=EpistemicStatus.user_stated,
        valid_from="2026-07-02T00:00:00+00:00",
        created_at="2026-07-02T00:00:00+00:00",
        decision_type=None,
    )
    request = RetrievalRequest(entity="ranking_recency", scope="global")
    forward = m4_memory.memory.retrieve(request)
    original_search = m4_memory.memory._store.search_beliefs

    def reverse_search(**filters):
        return list(reversed(original_search(**filters)))

    monkeypatch.setattr(m4_memory.memory._store, "search_beliefs", reverse_search)
    reverse = m4_memory.memory.retrieve(request)

    expected = [newer.id, older.id]
    assert [item.belief.id for item in forward.items] == expected
    assert [item.belief.id for item in reverse.items] == expected


def test_ranking_immutable_id_is_final_tie_breaker_under_reversed_input(
    m4_memory, monkeypatch
):
    first = m4_memory.add(
        entity="ranking_id", attribute="signal", value="first", decision_type=None
    )
    second = m4_memory.add(
        entity="ranking_id",
        attribute="signal",
        value="second",
        source_id="manager",
        decision_type=None,
    )
    request = RetrievalRequest(entity="ranking_id", scope="global")
    original_search = m4_memory.memory._store.search_beliefs

    def reverse_search(**filters):
        return list(reversed(original_search(**filters)))

    monkeypatch.setattr(m4_memory.memory._store, "search_beliefs", reverse_search)
    result = m4_memory.memory.retrieve(request)

    assert [item.belief.id for item in result.items] == [first.id, second.id]


def test_dedup_collapses_only_genuine_same_source_restatements(m4_memory):
    representative = m4_memory.add(value="paid")
    duplicate = m4_memory.add(value="  PAID  ")
    distinct_source = m4_memory.add(
        value="paid", source_id="manager", status=EpistemicStatus.user_stated
    )
    distinct_status = m4_memory.add(value="paid", status=EpistemicStatus.mentioned)
    conflict = m4_memory.add(value="FAILED")

    result = m4_memory.memory.retrieve(RetrievalRequest(scope="global"))
    ids = {item.belief.id for item in result.items}
    assert representative.id in ids
    assert duplicate.id not in ids
    assert {distinct_source.id, distinct_status.id, conflict.id}.issubset(ids)
    assert len(result.deduplications) == 1
    decision = result.deduplications[0]
    assert decision.rule_id == "R-DEDUP-001"
    assert decision.representative_id == representative.id
    assert decision.dropped_ids == [duplicate.id]


def test_dedup_preserves_supersession_lineage_metadata(m4_memory):
    root = m4_memory.add(
        entity="profile", attribute="theme", value="old", decision_type=None
    )
    versioned = m4_memory.memory._store.supersede(
        root.id,
        m4_memory.make(
            entity="profile", attribute="theme", value="dark", decision_type=None,
            valid_from="2026-07-02T00:00:00+00:00",
            created_at="2026-07-02T00:00:00+00:00",
        ),
    )
    unversioned = m4_memory.add(
        entity="profile", attribute="theme", value="dark", decision_type=None,
        valid_from="2026-07-02T00:00:00+00:00",
        created_at="2026-07-02T00:00:00+00:00",
    )

    result = m4_memory.memory.retrieve(RetrievalRequest(
        scope="global", entity="profile", attribute="theme"
    ))
    assert {item.belief.id for item in result.items} == {versioned.id, unversioned.id}
    assert not any(
        versioned.id == decision.representative_id
        or versioned.id in decision.dropped_ids
        for decision in result.deduplications
    )


def test_fts_startup_backfills_legacy_rows_idempotently_without_mutation(
    tmp_path
):
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE sources (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, label TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE beliefs (
            id INTEGER PRIMARY KEY, entity TEXT NOT NULL, attribute TEXT NOT NULL,
            value TEXT NOT NULL, status TEXT NOT NULL, scope TEXT NOT NULL,
            source_id TEXT NOT NULL REFERENCES sources(id), event_id INTEGER,
            supersedes_id INTEGER REFERENCES beliefs(id), decision_type TEXT,
            valid_from TEXT NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO sources VALUES
            ('user', 'user', 'Legacy user', '2026-07-01T00:00:00+00:00');
        INSERT INTO beliefs VALUES
            (41, 'legacy', 'note', 'LEGACY_OLD', 'user_stated', 'global',
             'user', NULL, NULL, NULL, '2026-07-01T00:00:00+00:00',
             '2026-07-01T00:00:00+00:00'),
            (42, 'legacy', 'note', 'LEGACY_CURRENT', 'user_stated', 'global',
             'user', NULL, 41, NULL, '2026-07-02T00:00:00+00:00',
             '2026-07-02T00:00:00+00:00'),
            (43, 'legacy', 'secret', 'LEGACY_HOBBY_SECRET', 'user_stated',
             'project:hobby', 'user', NULL, NULL, NULL,
             '2026-07-03T00:00:00+00:00', '2026-07-03T00:00:00+00:00');
        """
    )
    before = legacy.execute("SELECT * FROM beliefs ORDER BY id").fetchall()
    legacy.commit()
    legacy.close()

    policy = load_policy(str(Path(__file__).parents[1] / "trust_policy.yaml"))
    request = RetrievalRequest(query="LEGACY", scope="project:banking")

    first_store = MemoryStore(str(db_path), policy, agent_id="support-agent")
    try:
        first = first_store.retrieve(request)
        after_first = first_store._store.conn.execute(
            "SELECT * FROM beliefs ORDER BY id"
        ).fetchall()
    finally:
        first_store.close()

    second_store = MemoryStore(str(db_path), policy, agent_id="support-agent")
    try:
        second = second_store.retrieve(request)
        fts_rows = second_store._store.conn.execute(
            "SELECT rowid FROM beliefs_fts ORDER BY rowid"
        ).fetchall()
    finally:
        second_store.close()

    assert [item.belief.id for item in first.items] == [42]
    assert [item.belief.id for item in second.items] == [42]
    assert [tuple(row) for row in after_first] == before
    assert [row[0] for row in fts_rows] == [41, 42, 43]
    assert exclusion_count(first, "R-CURRENT-001") == 1
    assert exclusion_count(first, "R-SCOPE-TASK-001") == 1
