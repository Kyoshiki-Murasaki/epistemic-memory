"""Deterministic three-run scoring and reporting."""

from __future__ import annotations

from statistics import fmean, pvariance

from .adapters.base import BenchmarkAdapter
from .case_loader import load_cases
from .models import (
    BenchmarkCase,
    BenchmarkReport,
    CaseResult,
    DIMENSIONS,
    DimensionResult,
    Observation,
    RUN_COUNT,
    RunResult,
    ScoreStatistics,
)


class HarnessError(RuntimeError):
    """The harness or adapter could not produce a valid benchmark observation."""


def _statistics(values: list[float]) -> ScoreStatistics:
    if not values:
        raise HarnessError("cannot calculate statistics for zero runs")
    return ScoreStatistics(
        mean=fmean(values),
        minimum=min(values),
        maximum=max(values),
        variance=pvariance(values),
    )


def _run_once(
    adapter: BenchmarkAdapter,
    cases: tuple[BenchmarkCase, ...],
    run_number: int,
) -> RunResult:
    case_results = []
    for case in cases:
        try:
            observed = adapter.evaluate(case)
        except Exception as exc:
            raise HarnessError(f"adapter failed to evaluate case {case.case_id}") from exc
        if not isinstance(observed, Observation):
            raise HarnessError(f"adapter returned an invalid observation for {case.case_id}")
        case_results.append(CaseResult(
            case_id=case.case_id,
            dimension=case.dimension,
            passed=observed == case.expected,
        ))

    dimensions = []
    for dimension in DIMENSIONS:
        selected = [result for result in case_results if result.dimension == dimension]
        if not selected:
            raise HarnessError(f"dimension has zero cases: {dimension.value}")
        passed_cases = sum(result.passed for result in selected)
        total_cases = len(selected)
        dimensions.append(DimensionResult(
            dimension=dimension,
            passed_cases=passed_cases,
            failed_cases=total_cases - passed_cases,
            total_cases=total_cases,
            passed=passed_cases == total_cases,
        ))
    return RunResult(
        run_number=run_number,
        cases=tuple(case_results),
        dimensions=tuple(dimensions),
        overall_passed=all(result.passed for result in dimensions),
    )


def run_benchmark(
    adapter: BenchmarkAdapter,
    cases: tuple[BenchmarkCase, ...] | None = None,
) -> BenchmarkReport:
    selected = cases if cases is not None else load_cases()
    selected = tuple(sorted(
        selected,
        key=lambda case: (DIMENSIONS.index(case.dimension), case.case_id),
    ))
    runs = tuple(
        _run_once(adapter, selected, run_number)
        for run_number in range(1, RUN_COUNT + 1)
    )
    dimension_statistics = tuple(
        (
            dimension,
            _statistics([
                next(
                    result.score_percent
                    for result in run.dimensions
                    if result.dimension == dimension
                )
                for run in runs
            ]),
        )
        for dimension in DIMENSIONS
    )
    correctness_shapes = {
        tuple(result.passed for result in run.cases)
        for run in runs
    }
    return BenchmarkReport(
        adapter_name=adapter.name,
        runs=runs,
        dimension_statistics=dimension_statistics,
        overall_statistics=_statistics([run.score_percent for run in runs]),
        correctness_differs=len(correctness_shapes) != 1,
        overall_passed=all(run.overall_passed for run in runs),
    )
