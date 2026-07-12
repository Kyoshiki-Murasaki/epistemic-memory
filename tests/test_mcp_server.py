"""M8 official-client integration, schema, lifecycle, and security tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from epistemic_memory.core import MemoryStore
from epistemic_memory.mcp_server import HostConfig, TOOL_NAMES, create_server
from epistemic_memory.models import (
    AssemblyRequest,
    CandidateBelief,
    RetrievalRequest,
    Source,
)
from epistemic_memory.policy import load_policy


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = str(ROOT / "trust_policy.yaml")
PYTHON = sys.executable


def _candidate(
    value: str = "paid",
    *,
    entity: str = "order_4411",
    attribute: str = "payment_status",
    scope: str = "global",
    status: str = "system_verified",
) -> dict:
    return {
        "entity": entity,
        "attribute": attribute,
        "value": value,
        "proposed_status": status,
        "scope": scope,
        "decision_type": attribute,
    }


def _create_database(path: Path) -> None:
    memory = MemoryStore(
        str(path), load_policy(POLICY_PATH), agent_id="support-agent"
    )
    for source_id, source_type in (
        ("support-agent", "agent_inference"),
        ("user", "user"),
        ("billing", "billing_system"),
    ):
        memory._store.add_source(Source(
            id=source_id,
            type=source_type,
            label=source_id,
            created_at="2026-07-12T00:00:00+00:00",
        ))
    memory.close()


def _seed_belief(
    path: Path,
    *,
    source_id: str = "support-agent",
    value: str = "paid",
    scope: str = "global",
    entity: str = "order_4411",
    attribute: str = "payment_status",
):
    memory = MemoryStore(
        str(path), load_policy(POLICY_PATH), agent_id="support-agent"
    )
    candidate = CandidateBelief.model_validate(_candidate(
        value,
        entity=entity,
        attribute=attribute,
        scope=scope,
        status="ai_inferred" if source_id == "support-agent" else "user_stated",
    ))
    result = memory.ingest(
        source_id=source_id,
        content=f"seed:{value}",
        scope=scope,
        extractor=lambda _event, _source_type: [candidate],
    )
    memory.close()
    return result


def _stdio_parameters(
    path: Path,
    *,
    mode: str = "direct",
    agent_id: str = "support-agent",
    session_id: str = "mcp-test-session",
) -> StdioServerParameters:
    return StdioServerParameters(
        command=PYTHON,
        args=[
            "-m",
            "epistemic_memory.mcp_server",
            "--db",
            str(path),
            "--policy",
            POLICY_PATH,
            "--agent-id",
            agent_id,
            "--session-mode",
            mode,
            "--session-id",
            session_id,
        ],
        cwd=ROOT,
    )


async def _with_stdio(path: Path, callback, **parameters):
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as error_log:
        async with stdio_client(
            _stdio_parameters(path, **parameters), errlog=error_log
        ) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                value = await callback(session, initialized)
        error_log.seek(0)
        return value, error_log.read()


def _structured(result) -> dict:
    assert result.isError is False
    assert result.structuredContent is not None
    return result.structuredContent


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_official_client_discovery_schema_and_protocol_hygiene(tmp_path):
    path = tmp_path / "discovery.db"
    _create_database(path)

    async def scenario(session, initialized):
        listed = await session.list_tools()
        snapshot = {
            tool.name: {
                "properties": sorted(tool.inputSchema["properties"]),
                "required": sorted(tool.inputSchema.get("required", [])),
                "additionalProperties": tool.inputSchema.get("additionalProperties"),
            }
            for tool in listed.tools
        }
        assert snapshot == {
            "memory_ingest": {
                "properties": ["candidates", "content", "meta", "scope", "source_id"],
                "required": ["content", "scope", "source_id"],
                "additionalProperties": False,
            },
            "memory_retrieve": {
                "properties": ["attribute", "entity", "query", "scope", "status_floor", "task_type"],
                "required": ["scope"],
                "additionalProperties": False,
            },
            "memory_assemble_context": {
                "properties": ["attribute", "entity", "query", "scope", "status_floor", "task_type", "token_budget"],
                "required": ["scope"],
                "additionalProperties": False,
            },
            "memory_gate_action": {
                "properties": ["action", "entity", "scope", "task_type"],
                "required": ["action", "entity", "scope"],
                "additionalProperties": False,
            },
            "memory_explain": {
                "properties": ["belief_id", "scope", "task_type", "trace_id"],
                "required": ["scope", "trace_id"],
                "additionalProperties": False,
            },
            "memory_correct": {
                "properties": ["belief_id", "content", "kind", "proposed_status", "scope", "task_type", "value"],
                "required": ["belief_id", "content", "kind", "scope"],
                "additionalProperties": False,
            },
        }
        assert tuple(tool.name for tool in listed.tools) == TOOL_NAMES
        for tool in listed.tools:
            assert tool.description
            assert "host" in tool.description.lower()
            assert tool.outputSchema is not None
        assert initialized.capabilities.resources is None
        assert initialized.capabilities.prompts is None
        return listed

    (_, stderr) = asyncio.run(_with_stdio(path, scenario))
    assert stderr == ""


def test_schemas_exclude_every_trusted_or_forged_control(tmp_path):
    path = tmp_path / "forbidden.db"
    _create_database(path)

    async def scenario(session, _initialized):
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        forbidden = {
            "agent_id",
            "database_path",
            "db_path",
            "policy_path",
            "session_mode",
            "session_id",
            "approval_actor_id",
            "clock",
            "id_factory",
            "created_at",
            "valid_from",
        }
        for tool in tools.values():
            assert forbidden.isdisjoint(tool.inputSchema["properties"])
        assert "source_id" not in tools["memory_correct"].inputSchema["properties"]
        assert "decision_type" not in tools["memory_gate_action"].inputSchema["properties"]
        assert tools["memory_retrieve"].annotations.readOnlyHint is True
        assert tools["memory_explain"].annotations.readOnlyHint is True
        for name in TOOL_NAMES:
            if name not in {"memory_retrieve", "memory_explain"}:
                annotations = tools[name].annotations
                assert annotations is None or annotations.readOnlyHint is not True

    asyncio.run(_with_stdio(path, scenario))


def test_direct_mode_maps_all_six_tools_to_domain_results(tmp_path):
    path = tmp_path / "direct.db"
    _create_database(path)

    async def scenario(session, _initialized):
        ingested = _structured(await session.call_tool("memory_ingest", {
            "source_id": "support-agent",
            "content": "I infer payment was made",
            "scope": "global",
            "candidates": [_candidate(status="system_verified")],
        }))
        assert ingested["ok"] is True
        assert ingested["result_code"] == "beliefs_committed"
        assert ingested["trace_id"] == ingested["data"]["trace_id"]
        belief = ingested["data"]["beliefs"][0]
        assert belief["status"] == "ai_inferred"

        retrieved = _structured(await session.call_tool("memory_retrieve", {
            "entity": "order_4411", "scope": "global"
        }))
        assert retrieved["ok"] is True
        assert [item["belief"]["id"] for item in retrieved["data"]["items"]] == [belief["id"]]

        assembled = _structured(await session.call_tool("memory_assemble_context", {
            "entity": "order_4411", "scope": "global", "token_budget": 2000
        }))
        assert assembled["ok"] is True
        assert assembled["data"]["receipt"]
        assert assembled["data"]["permissions"]
        assert assembled["data"]["tokens_injected"] > 0
        assert assembled["trace_id"]

        gated = _structured(await session.call_tool("memory_gate_action", {
            "action": "acknowledge_claim",
            "entity": "order_4411",
            "scope": "global",
        }))
        assert gated["ok"] is True
        assert gated["data"]["decision"] == "allow"
        assert gated["trace_id"]
        denied_gate = _structured(await session.call_tool("memory_gate_action", {
            "action": "issue_refund",
            "entity": "order_4411",
            "scope": "global",
        }))
        assert denied_gate["ok"] is True
        assert denied_gate["data"]["decision"] == "deny"
        assert denied_gate["result_code"] == "gate_evaluated"

        explained = _structured(await session.call_tool("memory_explain", {
            "trace_id": assembled["trace_id"],
            "scope": "global",
            "belief_id": belief["id"],
        }))
        assert explained["ok"] is True
        assert explained["result_code"] == "explained"
        assert explained["data"]["counterfactual"] is not None

        corrected = _structured(await session.call_tool("memory_correct", {
            "belief_id": belief["id"],
            "kind": "correction",
            "content": "Corrected payment state",
            "scope": "global",
            "value": "unpaid",
            "proposed_status": "ai_inferred",
        }))
        assert corrected["ok"] is True
        assert corrected["result_code"] == "correction_applied"
        assert corrected["data"]["belief"]["supersedes_id"] == belief["id"]
        assert corrected["trace_id"]

        user_ingest = _structured(await session.call_tool("memory_ingest", {
            "source_id": "user",
            "content": "user-owned provenance",
            "scope": "global",
            "candidates": [_candidate(
                "Delhi", entity="customer", attribute="current_city",
                status="user_stated",
            )],
        }))
        denied_correction = _structured(await session.call_tool("memory_correct", {
            "belief_id": user_ingest["data"]["beliefs"][0]["id"],
            "kind": "retraction",
            "content": "cannot forge user provenance",
            "scope": "global",
        }))
        assert denied_correction["result_code"] == "source_write_not_permitted"

    (_, stderr) = asyncio.run(_with_stdio(path, scenario, session_id="direct-session"))
    assert stderr == ""


def test_direct_adapter_matches_public_memory_store_on_equivalent_fixture(tmp_path):
    mcp_path = tmp_path / "mcp-equivalent.db"
    core_path = tmp_path / "core-equivalent.db"
    _create_database(mcp_path)
    _create_database(core_path)
    candidate_data = _candidate(status="system_verified")

    async def scenario(session, _initialized):
        ingested = _structured(await session.call_tool("memory_ingest", {
            "source_id": "user",
            "content": "I already paid",
            "scope": "global",
            "candidates": [candidate_data],
        }))
        retrieved = _structured(await session.call_tool("memory_retrieve", {
            "entity": "order_4411", "scope": "global"
        }))
        gated = _structured(await session.call_tool("memory_gate_action", {
            "action": "confirm_payment", "entity": "order_4411", "scope": "global"
        }))
        return ingested, retrieved, gated

    ((mcp_ingest, mcp_retrieve, mcp_gate), _) = asyncio.run(
        _with_stdio(mcp_path, scenario)
    )

    core = MemoryStore(
        str(core_path), load_policy(POLICY_PATH), agent_id="support-agent"
    )
    candidate = CandidateBelief.model_validate(candidate_data)
    try:
        core_ingest = core.ingest(
            source_id="user",
            content="I already paid",
            scope="global",
            extractor=lambda _event, _source_type: [candidate],
        )
        core_retrieve = core.retrieve(RetrievalRequest(
            entity="order_4411", scope="global"
        ))
        core_gate = core.gate(
            action="confirm_payment", entity="order_4411", scope="global"
        )
    finally:
        core.close()

    mcp_belief = mcp_ingest["data"]["beliefs"][0]
    core_belief = core_ingest.beliefs[0]
    assert (
        mcp_belief["entity"],
        mcp_belief["attribute"],
        mcp_belief["value"],
        mcp_belief["status"],
        mcp_belief["scope"],
        mcp_belief["source_id"],
    ) == (
        core_belief.entity,
        core_belief.attribute,
        core_belief.value,
        core_belief.status.value,
        core_belief.scope,
        core_belief.source_id,
    )
    assert mcp_retrieve["data"]["authorized"] == core_retrieve.authorized
    assert mcp_retrieve["data"]["items"][0]["admitted_by"] == (
        core_retrieve.items[0].admitted_by
    )
    assert mcp_gate["data"]["decision"] == core_gate.decision.value
    assert mcp_gate["data"]["reason_codes"] == core_gate.reason_codes
    assert mcp_gate["data"]["rule_ids"] == core_gate.rule_ids


def test_propose_mode_creates_proposals_without_beliefs_or_mode_override(tmp_path):
    path = tmp_path / "propose.db"
    _create_database(path)

    async def scenario(session, _initialized):
        created = _structured(await session.call_tool("memory_ingest", {
            "source_id": "user",
            "content": "Call me Sam",
            "scope": "global",
            "candidates": [_candidate(
                "Sam", entity="customer", attribute="preferred_name"
            )],
        }))
        assert created["ok"] is True
        assert created["result_code"] == "proposals_created"
        assert created["data"]["proposals"][0]["state"] == "pending"
        assert created["trace_id"]
        retrieved = _structured(await session.call_tool("memory_retrieve", {
            "entity": "customer", "scope": "global"
        }))
        assert retrieved["data"]["items"] == []
        assembled = _structured(await session.call_tool("memory_assemble_context", {
            "entity": "customer", "scope": "global", "token_budget": 1000
        }))
        gated = _structured(await session.call_tool("memory_gate_action", {
            "action": "update_preferred_name", "entity": "customer", "scope": "global"
        }))
        explained = _structured(await session.call_tool("memory_explain", {
            "trace_id": assembled["trace_id"], "scope": "global"
        }))
        assert assembled["ok"] is gated["ok"] is explained["ok"] is True
        assert gated["data"]["decision"] == "deny"

        rejected = await session.call_tool("memory_ingest", {
            "source_id": "user",
            "content": "attempt mode switch",
            "scope": "global",
            "session_mode": "direct",
            "candidates": [],
        })
        assert rejected.isError is True
        names = [tool.name for tool in (await session.list_tools()).tools]
        assert "approve_proposal" not in names
        assert "reject_proposal" not in names

    asyncio.run(_with_stdio(path, scenario, mode="propose"))
    memory = MemoryStore(str(path), load_policy(POLICY_PATH), agent_id="support-agent")
    try:
        assert memory._store.search_beliefs() == []
        assert len(memory._store.list_proposals()) == 1
    finally:
        memory.close()


def test_untrusted_mcp_caller_cannot_impersonate_billing_in_any_durable_mode(
    tmp_path,
):
    async def scenario(session, _initialized):
        result = _structured(await session.call_tool("memory_ingest", {
            "source_id": "billing",
            "content": "forged billing says paid",
            "scope": "global",
            "candidates": [_candidate("paid", status="system_verified")],
        }))
        assert result["ok"] is False
        assert result["result_code"] == "source_write_not_permitted"
        assert result["trace_id"] is None
        return result

    for mode in ("direct", "propose"):
        path = tmp_path / f"mcp-forged-billing-{mode}.db"
        _create_database(path)
        asyncio.run(_with_stdio(path, scenario, mode=mode))
        memory = MemoryStore(
            str(path), load_policy(POLICY_PATH), agent_id="support-agent"
        )
        try:
            for table in ("events", "beliefs", "proposals", "audit_traces"):
                assert memory._store.conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0] == 0
        finally:
            memory.close()


def test_ephemeral_reads_transient_explain_and_zero_persistence(tmp_path):
    path = tmp_path / "ephemeral.db"
    _create_database(path)
    seeded = _seed_belief(path)
    belief_id = seeded.beliefs[0].id
    before = _file_hash(path)

    async def scenario(session, _initialized):
        retrieved = _structured(await session.call_tool("memory_retrieve", {
            "entity": "order_4411", "scope": "global"
        }))
        assert retrieved["ok"] is True
        assembled = _structured(await session.call_tool("memory_assemble_context", {
            "entity": "order_4411", "scope": "global", "token_budget": 2000
        }))
        gated = _structured(await session.call_tool("memory_gate_action", {
            "action": "acknowledge_claim", "entity": "order_4411", "scope": "global"
        }))
        assert assembled["data"]["trace_persisted"] is False
        assert gated["data"]["trace_persisted"] is False
        explained = _structured(await session.call_tool("memory_explain", {
            "trace_id": assembled["trace_id"], "scope": "global", "belief_id": belief_id
        }))
        assert explained["result_code"] == "explained"
        blocked_ingest = _structured(await session.call_tool("memory_ingest", {
            "source_id": "support-agent",
            "content": "blocked",
            "scope": "global",
            "candidates": [_candidate("changed", status="ai_inferred")],
        }))
        blocked_correct = _structured(await session.call_tool("memory_correct", {
            "belief_id": belief_id,
            "kind": "retraction",
            "content": "blocked",
            "scope": "global",
        }))
        assert blocked_ingest["result_code"] == "ephemeral_write_blocked"
        assert blocked_correct["result_code"] == "ephemeral_write_blocked"
        return assembled["trace_id"]

    (transient_id, stderr) = asyncio.run(_with_stdio(
        path, scenario, mode="ephemeral", session_id="ephemeral-session"
    ))
    assert stderr == ""
    assert _file_hash(path) == before

    async def restarted(session, _initialized):
        result = _structured(await session.call_tool("memory_explain", {
            "trace_id": transient_id, "scope": "global"
        }))
        assert result["ok"] is False
        assert result["result_code"] == "trace_unavailable"

    asyncio.run(_with_stdio(
        path, restarted, mode="ephemeral", session_id="ephemeral-restart"
    ))


def test_validation_forbidden_fields_and_malformed_values_fail_deterministically(tmp_path):
    path = tmp_path / "validation.db"
    _create_database(path)

    async def scenario(session, _initialized):
        invalid_calls = [
            ("memory_retrieve", {"scope": "global", "agent_id": "support-agent"}),
            ("memory_gate_action", {
                "action": "issue_refund", "entity": "order_4411", "scope": "global",
                "decision_type": "preferred_name",
            }),
            ("memory_correct", {
                "belief_id": 1, "kind": "retraction", "content": "x", "scope": "global",
                "source_id": "billing",
            }),
            ("memory_assemble_context", {"scope": "global", "token_budget": 0}),
            ("memory_explain", {"trace_id": "x", "scope": "global", "belief_id": 0}),
            ("memory_ingest", {
                "source_id": "user", "content": "x", "scope": "global",
                "candidates": [_candidate(status="not-a-status")],
            }),
        ]
        for name, arguments in invalid_calls:
            result = await session.call_tool(name, arguments)
            assert result.isError is True
        invalid_scope = _structured(await session.call_tool("memory_retrieve", {
            "scope": "not-a-scope"
        }))
        assert invalid_scope["result_code"] == "validation_error"

    asyncio.run(_with_stdio(path, scenario))


def test_domain_denials_and_hidden_scope_never_leak_content(tmp_path):
    path = tmp_path / "hidden.db"
    _create_database(path)
    marker = "HIDDEN-MCP-MARKER-9374"
    seeded = _seed_belief(
        path,
        value=marker,
        scope="project:secret",
        entity="secret_customer",
        attribute="current_city",
    )
    owner = MemoryStore(str(path), load_policy(POLICY_PATH), agent_id="support-agent")
    assembly = owner.assemble(AssemblyRequest(
        entity="secret_customer", scope="project:secret", token_budget=2000
    ))
    owner.close()

    async def scenario(session, _initialized):
        retrieve = _structured(await session.call_tool("memory_retrieve", {
            "query": "secret customer city", "scope": "global"
        }))
        explain = _structured(await session.call_tool("memory_explain", {
            "trace_id": assembly.trace_id, "scope": "global"
        }))
        correct = _structured(await session.call_tool("memory_correct", {
            "belief_id": seeded.beliefs[0].id,
            "kind": "retraction",
            "content": "try hidden correction",
            "scope": "global",
        }))
        serialized = json.dumps([retrieve, explain, correct], sort_keys=True)
        assert marker not in serialized
        assert retrieve["ok"] is True
        assert explain["result_code"] == "trace_unavailable"
        assert correct["result_code"] == "operation_not_permitted"

    asyncio.run(_with_stdio(path, scenario, agent_id="analytics-bot"))


def test_concurrent_calls_are_serialized_with_deterministic_mutation_order(tmp_path):
    path = tmp_path / "concurrent.db"
    _create_database(path)

    async def scenario(session, _initialized):
        first = asyncio.create_task(session.call_tool("memory_ingest", {
            "source_id": "support-agent",
            "content": "first",
            "scope": "global",
            "candidates": [_candidate("first", status="ai_inferred")],
        }))
        await asyncio.sleep(0)
        second = asyncio.create_task(session.call_tool("memory_ingest", {
            "source_id": "support-agent",
            "content": "second",
            "scope": "global",
            "candidates": [_candidate("second", status="ai_inferred")],
        }))
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first, second), timeout=5
        )
        first_data = _structured(first_result)["data"]
        second_data = _structured(second_result)["data"]
        assert [first_data["event"]["id"], second_data["event"]["id"]] == [1, 2]
        assert second_data["beliefs"][0]["supersedes_id"] == first_data["beliefs"][0]["id"]

        assemble_task = asyncio.create_task(session.call_tool(
            "memory_assemble_context",
            {"entity": "order_4411", "scope": "global", "token_budget": 2000},
        ))
        gate_task = asyncio.create_task(session.call_tool(
            "memory_gate_action",
            {"action": "acknowledge_claim", "entity": "order_4411", "scope": "global"},
        ))
        assembled, gated = [
            _structured(value)
            for value in await asyncio.wait_for(
                asyncio.gather(assemble_task, gate_task), timeout=5
            )
        ]
        assert assembled["trace_id"] != gated["trace_id"]
        explanations = []
        for trace_id in (assembled["trace_id"], gated["trace_id"]):
            explanations.append(_structured(await session.call_tool("memory_explain", {
                "trace_id": trace_id, "scope": "global"
            })))
        assert {
            value["data"]["trace"]["session_id"] for value in explanations
        } == {"concurrent-session"}

    (_, stderr) = asyncio.run(_with_stdio(
        path, scenario, session_id="concurrent-session"
    ))
    assert "SQLite" not in stderr


async def _with_in_memory_server(server, callback):
    client_send, server_receive = anyio.create_memory_object_stream(0)
    server_send, client_receive = anyio.create_memory_object_stream(0)
    task = asyncio.create_task(server.run(
        server_receive,
        server_send,
        server.create_initialization_options(),
    ))
    async with client_send, client_receive, ClientSession(
        client_receive, client_send
    ) as session:
        await session.initialize()
        result = await callback(session)
    await task
    return result


def test_one_store_per_session_and_close_on_shutdown(tmp_path):
    path = tmp_path / "lifecycle.db"
    _create_database(path)
    counts = {"opened": 0, "closed": 0}

    class CountingMemory(MemoryStore):
        def __init__(self, *args, **kwargs):
            counts["opened"] += 1
            super().__init__(*args, **kwargs)

        def close(self):
            counts["closed"] += 1
            super().close()

    server = create_server(
        HostConfig(str(path), POLICY_PATH, "support-agent"),
        policy=load_policy(POLICY_PATH),
        memory_factory=CountingMemory,
    )

    async def scenario(session):
        await session.call_tool("memory_retrieve", {"scope": "global"})
        await session.call_tool("memory_retrieve", {"scope": "global"})
        assert counts == {"opened": 1, "closed": 0}

    asyncio.run(_with_in_memory_server(server, scenario))
    assert counts == {"opened": 1, "closed": 1}


def test_internal_fault_response_is_generic_and_non_leaking(tmp_path):
    path = tmp_path / "fault.db"
    _create_database(path)
    secret = f"SELECT secret FROM hidden; {path}"

    class FaultMemory(MemoryStore):
        def retrieve(self, request):
            raise RuntimeError(secret)

    server = create_server(
        HostConfig(str(path), POLICY_PATH, "support-agent"),
        policy=load_policy(POLICY_PATH),
        memory_factory=FaultMemory,
    )

    async def scenario(session):
        result = _structured(await session.call_tool(
            "memory_retrieve", {"scope": "global"}
        ))
        serialized = json.dumps(result)
        assert result["result_code"] == "internal_error"
        assert secret not in serialized
        assert str(path) not in serialized
        assert "Traceback" not in serialized

    asyncio.run(_with_in_memory_server(server, scenario))


def test_startup_help_import_and_both_entry_points_are_hygienic(tmp_path):
    missing = tmp_path / "must-not-exist.db"
    help_result = subprocess.run(
        [PYTHON, "-m", "epistemic_memory.mcp_server", "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert "usage:" in help_result.stdout
    assert not missing.exists()

    imported = subprocess.run(
        [PYTHON, "-c", "import epistemic_memory.mcp_server"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert imported.returncode == 0
    assert imported.stdout == imported.stderr == ""
    assert list(tmp_path.iterdir()) == []

    invalid = subprocess.run(
        [
            PYTHON,
            "-m",
            "epistemic_memory.mcp_server",
            "--db",
            str(missing),
            "--policy",
            POLICY_PATH,
            "--agent-id",
            "unknown-agent",
        ],
        cwd=ROOT,
        input="",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert "startup failed" in invalid.stderr

    console = Path(PYTHON).parent / "epistemic-memory-mcp"
    assert console.is_file()
    console_help = subprocess.run(
        [str(console), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert console_help.returncode == 0
    assert "usage:" in console_help.stdout
