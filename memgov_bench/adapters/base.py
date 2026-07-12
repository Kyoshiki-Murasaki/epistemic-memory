"""The canonical MemGov-Bench adapter boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import BenchmarkCase, Observation


@runtime_checkable
class BenchmarkAdapter(Protocol):
    """A participant executes one isolated case and returns typed observations."""

    name: str

    def evaluate(self, case: BenchmarkCase) -> Observation:
        """Execute ``case`` without consulting its expected observation."""
        ...
