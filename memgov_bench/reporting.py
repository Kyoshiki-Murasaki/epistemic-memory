"""Stable Markdown terminal rendering."""

from __future__ import annotations

from .models import BenchmarkReport


def _status(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def render_report(report: BenchmarkReport) -> str:
    lines = [
        "# MemGov-Bench",
        "",
        f"Adapter: `{report.adapter_name}`",
        f"Runs: {len(report.runs)} deterministic complete runs",
        "",
        "## Per-run results",
        "",
        "| Run | Dimension | Passed | Failed | Total | Result |",
        "|---:|---|---:|---:|---:|---|",
    ]
    for run in report.runs:
        for result in run.dimensions:
            lines.append(
                f"| {run.run_number} | {result.dimension.value} | "
                f"{result.passed_cases} | {result.failed_cases} | "
                f"{result.total_cases} | {_status(result.passed)} |"
            )
        lines.append(
            f"| {run.run_number} | **Overall** | {run.passed_cases} | "
            f"{run.total_cases - run.passed_cases} | {run.total_cases} | "
            f"**{_status(run.overall_passed)}** |"
        )
    lines.extend([
        "",
        "## Three-run statistics",
        "",
        "| Dimension | Mean % | Minimum % | Maximum % | Variance (pp²) |",
        "|---|---:|---:|---:|---:|",
    ])
    for dimension, stats in report.dimension_statistics:
        lines.append(
            f"| {dimension.value} | {stats.mean:.2f} | {stats.minimum:.2f} | "
            f"{stats.maximum:.2f} | {stats.variance:.4f} |"
        )
    stats = report.overall_statistics
    lines.extend([
        f"| **Overall case score** | {stats.mean:.2f} | {stats.minimum:.2f} | "
        f"{stats.maximum:.2f} | {stats.variance:.4f} |",
        "",
        "Correctness differed across runs: "
        + ("yes" if report.correctness_differs else "no"),
        "",
        "Overall: **"
        + _status(report.overall_passed)
        + "** (requires every case in every required dimension to pass in all three runs)",
        "",
    ])
    return "\n".join(lines)
