"""Command-line entry point for the canonical MemGov-Bench invocation."""

from __future__ import annotations

import argparse
import sys

from .adapters.ours import OursAdapter
from .reporting import render_report
from .runner import HarnessError, run_benchmark


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m memgov_bench",
        description="Run the deterministic five-dimension MemGov-Bench harness.",
    )
    parser.add_argument("--adapter", required=True, choices=("ours",))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapter = OursAdapter() if args.adapter == "ours" else None
    if adapter is None:
        return 2
    try:
        report = run_benchmark(adapter)
    except HarnessError as exc:
        print(f"MemGov-Bench harness error: {exc}", file=sys.stderr)
        return 2
    print(render_report(report), end="")
    return 0 if report.overall_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
