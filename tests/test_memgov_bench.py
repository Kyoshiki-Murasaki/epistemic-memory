"""Canonical M11 MemGov-Bench contract, validity, and reproducibility tests."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from memgov_bench.adapters.controls import AlwaysDenyAdapter
from memgov_bench.adapters.ours import OursAdapter
from memgov_bench.case_loader import load_cases
from memgov_bench.models import DIMENSIONS, RUN_COUNT, Dimension, Observation
from memgov_bench.reporting import render_report
from memgov_bench.runner import HarnessError, run_benchmark


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "memgov_bench" / "data" / "cases.json"
OURS_PATH = ROOT / "memgov_bench" / "adapters" / "ours.py"


@pytest.fixture(scope="module")
def cases():
    return load_cases()


@pytest.fixture(scope="module")
def ours_report():
    return run_benchmark(OursAdapter())


def _raw_cases():
    return json.loads(CASES_PATH.read_text())


def _write_cases(tmp_path, raw):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(raw))
    return path


def test_contract_has_exactly_five_canonical_dimensions_and_no_extra():
    assert tuple(dimension.value for dimension in DIMENSIONS) == (
        "stale-fact leakage",
        "claim/fact confusion",
        "scope leakage",
        "injection resistance",
        "gate correctness",
    )
    assert len(DIMENSIONS) == len(Dimension) == 5


def test_all_cases_parse_have_unique_ids_and_explicit_expected_outcomes(cases):
    assert len(cases) == 10
    assert len({case.case_id for case in cases}) == len(cases)
    assert {case.dimension for case in cases} == set(DIMENSIONS)
    assert all(case.expected is not None for case in cases)
    assert all(sum(case.dimension == dimension for case in cases) == 2 for dimension in DIMENSIONS)


@pytest.mark.parametrize("mutation", ["missing_expected", "duplicate_id", "missing_dimension"])
def test_invalid_fixture_contracts_fail(tmp_path, mutation):
    raw = _raw_cases()
    if mutation == "missing_expected":
        raw[0].pop("expected")
    elif mutation == "duplicate_id":
        raw[1]["case_id"] = raw[0]["case_id"]
    else:
        raw = [case for case in raw if case["dimension"] != "scope leakage"]
    with pytest.raises((ValidationError, ValueError)):
        load_cases(_write_cases(tmp_path, raw))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("script", "print('not inert')"),
        ("note", "/Users/example/private.db"),
        ("note", "API key: synthetic-but-forbidden"),
    ],
)
def test_executable_content_credentials_and_local_paths_are_rejected(tmp_path, key, value):
    raw = _raw_cases()
    raw[0][key] = value
    with pytest.raises(ValueError, match="fixture data"):
        load_cases(_write_cases(tmp_path, raw))


def test_fixture_is_original_synthetic_inert_data_without_personal_markers():
    text = CASES_PATH.read_text()
    assert "synthetic" in text.lower()
    assert not re.search(r"/Users/|/home/|sk-[A-Za-z0-9]|@example|https?://|^#!", text, re.M)
    raw = _raw_cases()
    assert not {"code", "command", "eval", "exec", "script"}.intersection(
        str(key).lower()
        for case in raw
        for key in case
    )


def test_expected_labels_load_without_ours_or_production_decision_calls(monkeypatch):
    import epistemic_memory.core as core_module

    def forbidden(*_args, **_kwargs):
        raise AssertionError("fixture loading called a production decision boundary")

    for name in ("ingest_event", "retrieve_beliefs", "assemble_context", "_gate", "_explain"):
        monkeypatch.setattr(core_module, name, forbidden)
    loaded = load_cases()
    assert loaded[0].expected


def test_ours_adapter_never_reads_expected_labels():
    tree = ast.parse(OURS_PATH.read_text())
    attributes = {
        (node.value.id, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    assert ("case", "expected") not in attributes


def test_report_formatting_cannot_rewrite_frozen_case_labels(cases, ours_report):
    before = tuple(case.expected.model_dump_json() for case in cases)
    render_report(ours_report)
    after = tuple(case.expected.model_dump_json() for case in cases)
    assert after == before


def test_ours_static_boundary_uses_memory_store_without_store_sql_or_network_clients():
    source = OURS_PATH.read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "MemoryStore" in imports
    assert "Store" not in imports
    assert "sqlite3" not in imports
    assert not re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE)\b", source, re.I)
    assert not {"requests", "httpx", "urllib", "socket", "anthropic"}.intersection(imports)
    assert "sleep(" not in source
    assert "datetime.now" not in source
    assert "uuid" not in source.lower()
    assert "os.environ" not in source


def test_only_store_imports_sqlite3_and_benchmark_imports_neither_private_store_nor_sqlite():
    importers = []
    for path in (ROOT / "epistemic_memory").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            (isinstance(node, ast.Import) and any(alias.name == "sqlite3" for alias in node.names))
            or (isinstance(node, ast.ImportFrom) and node.module == "sqlite3")
            for node in ast.walk(tree)
        ):
            importers.append(path.relative_to(ROOT).as_posix())
    assert importers == ["epistemic_memory/store.py"]
    for path in (ROOT / "memgov_bench").rglob("*.py"):
        source = path.read_text()
        assert "sqlite3" not in source
        assert not re.search(r"(?:from|import)\s+epistemic_memory\.store", source)


def test_ours_uses_fresh_temporary_state_and_never_mutates_a_caller_database(tmp_path, cases):
    sentinel = tmp_path / "user.db"
    sentinel.write_bytes(b"caller-owned-database-sentinel")
    before = sentinel.read_bytes()
    first = OursAdapter().evaluate(cases[0])
    second = OursAdapter().evaluate(cases[0])
    assert first == second == cases[0].expected
    assert sentinel.read_bytes() == before
    assert list(tmp_path.iterdir()) == [sentinel]


def test_ours_satisfies_exact_counts_dimension_and_overall_rules(ours_report):
    assert len(ours_report.runs) == RUN_COUNT == 3
    for run in ours_report.runs:
        assert [(item.passed_cases, item.failed_cases, item.total_cases, item.passed)
                for item in run.dimensions] == [(2, 0, 2, True)] * 5
        assert run.passed_cases == 10
        assert run.total_cases == 10
        assert run.overall_passed
    assert ours_report.overall_passed


class _OneFailureAdapter:
    name = "one-failure"

    def evaluate(self, case):
        observed = OursAdapter().evaluate(case)
        if case.case_id == "stale-current-control":
            return observed.model_copy(update={"retrieved": ()})
        return observed


def test_one_failed_required_dimension_cannot_be_hidden():
    report = run_benchmark(_OneFailureAdapter())
    assert all(run.passed_cases == 9 for run in report.runs)
    assert all(not run.dimensions[0].passed for run in report.runs)
    assert all(not run.overall_passed for run in report.runs)
    assert not report.overall_passed


def test_valid_benchmark_failure_is_distinct_from_harness_error():
    failed = run_benchmark(AlwaysDenyAdapter())
    assert not failed.overall_passed

    class ArbitraryOutput:
        name = "arbitrary"

        def evaluate(self, _case):
            return "success"

    with pytest.raises(HarnessError, match="invalid observation"):
        run_benchmark(ArbitraryOutput())


def test_no_zero_case_nan_or_infinite_scores(ours_report):
    values = []
    for run in ours_report.runs:
        assert all(result.total_cases > 0 for result in run.dimensions)
        values.extend(result.score_percent for result in run.dimensions)
    for _, stats in ours_report.dimension_statistics:
        values.extend((stats.mean, stats.minimum, stats.maximum, stats.variance))
    values.extend(vars(ours_report.overall_statistics).values())
    assert all(math.isfinite(value) for value in values)


def test_exactly_three_runs_record_every_result_and_zero_variance(ours_report):
    assert [run.run_number for run in ours_report.runs] == [1, 2, 3]
    assert all(len(run.cases) == 10 and len(run.dimensions) == 5 for run in ours_report.runs)
    for _, stats in ours_report.dimension_statistics:
        assert (stats.mean, stats.minimum, stats.maximum, stats.variance) == (100.0, 100.0, 100.0, 0.0)
    assert vars(ours_report.overall_statistics) == {
        "mean": 100.0,
        "minimum": 100.0,
        "maximum": 100.0,
        "variance": 0.0,
    }
    assert not ours_report.correctness_differs


def test_nonzero_three_run_statistics_are_calculated_correctly(cases):
    class SecondRunFailure:
        name = "second-run-failure"

        def __init__(self):
            self.calls = 0

        def evaluate(self, case):
            self.calls += 1
            if 11 <= self.calls <= 20 and case.case_id == "stale-current-control":
                return case.expected.model_copy(update={"retrieved": ()})
            return case.expected

    report = run_benchmark(SecondRunFailure(), cases)
    stale = dict(report.dimension_statistics)[Dimension.stale_fact_leakage]
    assert stale.mean == pytest.approx(250 / 3)
    assert stale.minimum == 50.0
    assert stale.maximum == 100.0
    assert stale.variance == pytest.approx(5000 / 9)
    assert report.overall_statistics.mean == pytest.approx(290 / 3)
    assert report.overall_statistics.variance == pytest.approx(200 / 9)
    assert report.correctness_differs
    assert not report.overall_passed


def test_reordered_cases_produce_same_sorted_results_and_scores(cases):
    canonical = run_benchmark(OursAdapter(), cases)
    reversed_report = run_benchmark(OursAdapter(), tuple(reversed(cases)))
    assert canonical == reversed_report


def test_negative_control_fails_every_dimension_and_legitimate_positive_controls(cases):
    report = run_benchmark(AlwaysDenyAdapter(), cases)
    assert all(not dimension.passed for dimension in report.runs[0].dimensions)
    results = {result.case_id: result.passed for result in report.runs[0].cases}
    for positive in (
        "claim-user-labelled",
        "claim-verified-control",
        "injection-trusted-control",
        "gate-sufficient-allowed",
    ):
        assert not results[positive]


def test_import_and_help_are_side_effect_free(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    for command in (
        [sys.executable, "-c", "import memgov_bench; import memgov_bench.__main__"],
        [sys.executable, "-m", "memgov_bench", "--help"],
    ):
        result = subprocess.run(command, cwd=tmp_path, env=env, text=True, capture_output=True)
        assert result.returncode == 0
        assert result.stderr == ""
        assert list(tmp_path.iterdir()) == []


def test_cli_works_is_byte_identical_and_omits_machine_specific_data(tmp_path):
    command = [sys.executable, "-m", "memgov_bench", "--adapter", "ours"]
    first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
    second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=True)
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    assert hashlib.sha256(first.stdout.encode()).hexdigest() == hashlib.sha256(second.stdout.encode()).hexdigest()
    assert "Runs: 3 deterministic complete runs" in first.stdout
    assert "Overall: **PASS**" in first.stdout
    forbidden = (str(ROOT), str(tmp_path), Path.home().name, os.uname().nodename, "Traceback")
    assert all(value not in first.stdout for value in forbidden)


def test_unknown_adapter_exits_nonzero_without_running_benchmark(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "memgov_bench", "--adapter", "unknown"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr
    assert list(tmp_path.iterdir()) == []
