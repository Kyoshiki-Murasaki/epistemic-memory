"""Boundary-safe deterministic rendering for untrusted dynamic fields."""

from __future__ import annotations

import json


def safe_text(value: object) -> str:
    """Return one JSON string literal so content cannot forge text sections."""
    return json.dumps(str(value), ensure_ascii=True)
