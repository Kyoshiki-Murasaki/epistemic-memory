"""M10 release-documentation and adoption-readiness verification."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

import pytest

from epistemic_memory.mcp_server import TOOL_NAMES, _tool_definitions
from epistemic_memory.policy import load_policy


ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
README = README_PATH.read_text()
EXAMPLES = ROOT / "examples"
DEMO_HASH = "4b49f0a69cb03bf8396feca897ce3e153087eba43b8a86b19874995db7c58fcc"
DEMO_EXCERPT = """STEP 4 — Refund request fails closed
Input
  "Issue refund for order 4411"
What happened
  decision="deny" risk="irreversible"
Why
  authoritative billing evidence says FAILED, not the policy-required paid value"""


@pytest.fixture(scope="module")
def demo_transcript() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "epistemic_memory.demo"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stderr == ""
    return result.stdout


def test_readme_opens_with_binding_mission_verbatim():
    spec = (ROOT / "docs" / "02_SPEC.md").read_text()
    mission = spec.split(
        "## Mission (this is the project's north star — put it verbatim in the README)\n",
        1,
    )[1].split("\n## One-sentence pitch", 1)[0].strip()
    quoted = "\n".join(
        line.removeprefix("> ")
        for line in README.split("\n\n", 2)[1].splitlines()
    )
    assert quoted == mission


def test_readme_references_current_install_and_console_commands():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert metadata["requires-python"] == ">=3.11"
    scripts = metadata["scripts"]
    assert scripts == {
        "epistemic-memory-mcp": "epistemic_memory.mcp_server:main",
        "epistemic-memory-demo": "epistemic_memory.demo:main",
    }
    for command in (
        "python -m pip install -e .",
        "python -m epistemic_memory.demo",
        "epistemic-memory-demo",
        "epistemic-memory-mcp --help",
        "python -m epistemic_memory.mcp_server --help",
        "python -m memgov_bench --adapter ours",
    ):
        assert command in README


def test_demo_command_hash_and_readme_excerpt_are_current(demo_transcript):
    assert hashlib.sha256(demo_transcript.encode()).hexdigest() == DEMO_HASH
    assert DEMO_HASH in README
    assert DEMO_EXCERPT in demo_transcript
    assert DEMO_EXCERPT in README
    assert 'RESULT "all demo invariants passed"' in demo_transcript


def test_readme_mcp_reference_matches_exact_six_runtime_tools():
    headings = tuple(re.findall(r"^#### `(memory_[a-z_]+)`$", README, re.MULTILINE))
    runtime = tuple(tool.name for tool in _tool_definitions())
    assert headings == TOOL_NAMES == runtime
    assert len(set(headings)) == 6


def test_no_unimplemented_mcp_tools_are_documented():
    forbidden = {
        "memory_approve_proposal",
        "memory_reject_proposal",
        "memory_add_commitment",
        "memory_register_artifact",
        "memory_register_dependency",
        "memory_add_source",
        "memory_update_policy",
        "memory_sql",
        "memory_audit_table",
    }
    assert forbidden.isdisjoint(set(re.findall(r"memory_[a-z_]+", README)))


def test_runtime_schemas_exclude_trusted_controls():
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
    tools = {tool.name: tool for tool in _tool_definitions()}
    for tool in tools.values():
        assert forbidden.isdisjoint(tool.inputSchema["properties"])
        assert tool.inputSchema["additionalProperties"] is False
    assert "source_id" not in tools["memory_correct"].inputSchema["properties"]
    assert "decision_type" not in tools["memory_gate_action"].inputSchema["properties"]


def test_least_privileged_policy_example_parses_and_validates():
    policy = load_policy(str(EXAMPLES / "trust_policy.yaml"))
    assert policy.source_principals == {"user": "user"}
    assert set(policy.source_status_ceiling) == {"user"}
    agent = policy.agents["support-agent"]
    assert agent.allowed_scopes == ["global"]
    assert agent.max_action_tier.value == "low_stakes"
    assert agent.ingest_source_ids == ["user"]
    assert agent.writable_source_ids == ["user"]


def test_python_quickstart_imports_and_runs_without_live_services():
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "python_quickstart.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stderr == ""
    summary = json.loads(result.stdout)
    assert summary == {
        "approved_value": "Rae",
        "correction_code": "correction_applied",
        "direct_gate": "allow",
        "ephemeral_write": "ephemeral_write_blocked",
        "proposal_code": "proposal_approved",
    }


def test_bootstrap_example_creates_reopenable_store(tmp_path):
    database = tmp_path / "memory.db"
    command = [
        sys.executable,
        str(EXAMPLES / "bootstrap_store.py"),
        "--db",
        str(database),
        "--policy",
        str(EXAMPLES / "trust_policy.yaml"),
    ]
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    assert database.is_file()


def test_mcp_json_example_is_valid_and_uses_host_startup_args():
    config = json.loads((EXAMPLES / "mcp_config.json").read_text())
    server = config["mcpServers"]["epistemic-memory"]
    assert server["type"] == "stdio"
    assert server["command"] == "epistemic-memory-mcp"
    assert server["args"] == [
        "--db",
        "${EPISTEMIC_MEMORY_DB}",
        "--policy",
        "${EPISTEMIC_MEMORY_POLICY}",
        "--agent-id",
        "support-agent",
        "--session-mode",
        "direct",
    ]
    assert "toolArguments" not in server


def test_architecture_and_read_only_claims_match_static_boundaries():
    mcp_path = ROOT / "epistemic_memory" / "mcp_server.py"
    mcp_tree = ast.parse(mcp_path.read_text())
    imported = {
        alias.name
        for node in ast.walk(mcp_tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "sqlite3" not in imported
    assert "Store" not in imported
    core_text = (ROOT / "epistemic_memory" / "core.py").read_text()
    store_text = (ROOT / "epistemic_memory" / "store.py").read_text()
    assert "from .store import Store" in core_text
    assert "?mode=ro" in store_text
    assert "PRAGMA query_only = ON" in store_text
    assert "The adapter calls the service boundary and never accesses SQLite directly" in README
    assert "atomic" in README.lower() and "read-only" in README.lower()


def test_sqlite_import_boundary_remains_narrow():
    importers = []
    for path in (ROOT / "epistemic_memory").glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if any(
            alias.name == "sqlite3"
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ):
            importers.append(path.name)
    assert importers == ["store.py"]


def test_readme_uses_durable_status_labels_without_brittle_test_count():
    assert "Implemented and tested" in README
    assert "Pilot limitation" in README
    assert "Not implemented" in README
    assert re.search(r"\b\d+ passed\b", README) is None


@pytest.mark.parametrize(
    "claim",
    [
        "production ready",
        "enterprise grade",
        "fully secure",
        "prevents hallucinations",
        "guarantees correctness",
        "guaranteed truth",
        "drop-in replacement for every memory system",
        "industry standard",
    ],
)
def test_readme_avoids_unsupported_release_claims(claim):
    assert claim not in README.lower()


def test_docs_contain_no_machine_paths_credentials_or_external_benchmark_claims():
    texts = [
        README,
        *(path.read_text() for path in sorted(EXAMPLES.iterdir()) if path.is_file()),
    ]
    combined = "\n".join(texts)
    assert "/Users/" not in combined
    assert "/home/" not in combined
    assert re.search(r"[A-Za-z]:\\\\Users\\\\", combined) is None
    assert re.search(r"\bsk-[A-Za-z0-9_-]{8,}", combined) is None
    assert "BEGIN PRIVATE KEY" not in combined
    assert "two independently labelled synthetic cases per dimension" in README
    assert "No external vendor adapter or model-based evaluation is implemented" in README
    assert not re.search(
        r"(?:Mem0|Zep|Letta).{0,80}(?:scored|passes|failed|outperform|superior)",
        combined,
        re.I,
    )


def test_local_markdown_links_resolve():
    targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", README)
    local = [target.split("#", 1)[0] for target in targets if "://" not in target]
    assert local
    missing = [target for target in local if not (ROOT / unquote(target)).exists()]
    assert missing == []


def test_code_fences_balance_and_bash_blocks_parse():
    assert README.count("```") % 2 == 0
    bash_blocks = re.findall(r"```bash\n(.*?)\n```", README, re.DOTALL)
    assert bash_blocks
    for block in bash_blocks:
        result = subprocess.run(
            ["bash", "-n"], input=block, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_examples_do_not_bypass_public_boundary_or_embed_sql():
    for path in (EXAMPLES / "bootstrap_store.py", EXAMPLES / "python_quickstart.py"):
        text = path.read_text()
        tree = ast.parse(text, filename=str(path))
        assert "._store" not in text
        assert "sqlite3" not in text
        assert not re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE)\b", text)
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "epistemic_memory.core"
            and any(alias.name == "MemoryStore" for alias in node.names)
            for node in ast.walk(tree)
        )


def test_temporary_editable_install_exposes_and_runs_documented_entry_points(tmp_path):
    environment = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "-e", str(ROOT)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    module = subprocess.run(
        [str(python), "-m", "epistemic_memory.demo"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert hashlib.sha256(module.stdout.encode()).hexdigest() == DEMO_HASH
    benchmark = subprocess.run(
        [str(python), "-m", "memgov_bench", "--adapter", "ours"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Overall: **PASS**" in benchmark.stdout
    assert benchmark.stderr == ""
    for script in ("epistemic-memory-demo", "epistemic-memory-mcp"):
        executable = bin_dir / script
        assert executable.is_file()
        help_result = subprocess.run(
            [str(executable), "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "usage:" in help_result.stdout
