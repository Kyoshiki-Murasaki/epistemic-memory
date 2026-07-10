"""MemoryStore — the project's ONLY public API (spec "foundation requirements" #1).

Every client (CLI, MCP server, demo, benchmark adapter) is a thin caller of this
class. It never touches SQLite itself — all storage goes through .store.Store.
Method bodies for ingest/retrieve/assemble/gate/correct land in M2-M7; this
milestone (M1) wires the class to the store and enforces the API boundary.
"""

from __future__ import annotations

from typing import Optional

from .ingest import Extractor, ingest_event, live_extractor
from .models import GateResult, IngestResult, TrustPolicy
from .policy import gate as _gate
from .store import Store


class MemoryStore:
    def __init__(
        self,
        db_path: str,
        policy: Optional[TrustPolicy] = None,
        *,
        agent_id: str,
        ephemeral: bool = False,
        propose: bool = False,
        live: bool = False,
    ):
        self._store = Store(db_path)
        self.policy = policy
        self.agent_id = agent_id
        self.ephemeral = ephemeral
        self.propose = propose
        self.live = live

    def close(self) -> None:
        self._store.close()

    # The six core verbs from 02_SPEC.md line 23. Implemented milestone by
    # milestone (M2 ingest, M4 retrieve/assemble, M3 gate, M6 correct, M7 explain).

    def ingest(
        self,
        *,
        source_id: str,
        content: str,
        scope: str,
        meta: Optional[dict] = None,
        extractor: Optional[Extractor] = None,
    ) -> IngestResult:
        if self.policy is None:
            raise ValueError("MemoryStore requires a policy to ingest (pass policy=load_policy(...))")
        if extractor is None:
            if not self.live:
                raise ValueError(
                    "no extractor supplied and live mode is off — pass extractor=... "
                    "(a fixture) or construct MemoryStore(..., live=True) with ANTHROPIC_API_KEY set"
                )
            extractor = live_extractor
        return ingest_event(
            self._store,
            self.policy,
            source_id=source_id,
            content=content,
            scope=scope,
            meta=meta,
            extractor=extractor,
        )

    def retrieve(
        self, *, query: str, entity: Optional[str] = None, scope: str,
        task_type: Optional[str] = None, status_floor: Optional[str] = None,
    ):
        raise NotImplementedError("retrieve lands in M4")

    def assemble(
        self, *, query: str, entity: Optional[str] = None, scope: str,
        task_type: Optional[str] = None, token_budget: int = 1024,
    ):
        raise NotImplementedError("assemble lands in M4")

    def gate(self, *, action: str, entity: str) -> GateResult:
        if self.policy is None:
            raise ValueError("MemoryStore requires a policy to gate (pass policy=load_policy(...))")
        if action not in self.policy.actions:
            raise ValueError(f"unknown action: {action!r}")
        attribute = self.policy.actions[action].decision
        supporting = self._store.current_beliefs(entity, attribute)
        source_types = {b.source_id: self._store.get_source(b.source_id).type for b in supporting}
        return _gate(action, supporting, self.policy, source_types, agent_id=self.agent_id)

    def correct(self, belief_id: int, *, reason: str):
        raise NotImplementedError("correct lands in M6")

    def explain(self, trace_id: int):
        raise NotImplementedError("explain lands in M7")

    def add_commitment(self, **kwargs):
        raise NotImplementedError("add_commitment lands in M5")
