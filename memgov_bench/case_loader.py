"""Load and validate inert, independently labelled benchmark fixtures."""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from .models import BenchmarkCase, DIMENSIONS


_CASES = TypeAdapter(tuple[BenchmarkCase, ...])
_FORBIDDEN_TEXT = re.compile(
    r"(?:^#!|\b(?:api[_ -]?key|password|secret)\b|sk-[A-Za-z0-9]|/Users/|/home/|[A-Za-z]:\\)",
    re.IGNORECASE,
)


def _validate_inert_data(value: Any) -> None:
    if isinstance(value, dict):
        forbidden_keys = {"code", "command", "eval", "exec", "script"}
        if forbidden_keys.intersection(str(key).lower() for key in value):
            raise ValueError("fixture data cannot contain executable fields")
        for key, item in value.items():
            _validate_inert_data(key)
            _validate_inert_data(item)
    elif isinstance(value, list):
        for item in value:
            _validate_inert_data(item)
    elif isinstance(value, str) and _FORBIDDEN_TEXT.search(value):
        raise ValueError("fixture data contains a forbidden path, credential, or executable marker")


def load_cases(path: Path | None = None) -> tuple[BenchmarkCase, ...]:
    if path is None:
        text = resources.files("memgov_bench.data").joinpath("cases.json").read_text(
            encoding="utf-8"
        )
    else:
        text = path.read_text(encoding="utf-8")
    raw = json.loads(text)
    _validate_inert_data(raw)
    cases = _CASES.validate_json(text, strict=True)
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark case IDs must be unique")
    dimensions = {case.dimension for case in cases}
    if dimensions != set(DIMENSIONS):
        raise ValueError("fixtures must cover exactly the five canonical dimensions")
    return tuple(sorted(cases, key=lambda case: (DIMENSIONS.index(case.dimension), case.case_id)))
