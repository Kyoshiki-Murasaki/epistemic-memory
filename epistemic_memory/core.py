"""MemoryStore — the project's ONLY public API (spec "foundation requirements" #1).

Every client (CLI, MCP server, demo, benchmark adapter) is a thin caller of this
class. It never touches SQLite itself — all storage goes through .store.Store.
Method bodies for ingest/retrieve/assemble/gate/correct land in M2-M7; this
milestone (M1) wires the class to the store and enforces the API boundary.
"""

from __future__ import annotations

from typing import Optional

from .assemble import assemble_context
from .commitments import (
    add_commitment as _add_commitment,
    list_commitments as _list_commitments,
    surface_overdue as _surface_overdue,
    transition_commitment as _transition_commitment,
)
from .ingest import Extractor, ingest_event, live_extractor
from .models import (
    AssembledContext,
    AssemblyRequest,
    CommitmentCreateRequest,
    CommitmentListRequest,
    CommitmentListResult,
    CommitmentMutationResult,
    CommitmentTransitionRequest,
    GateDecision,
    GateResult,
    IngestResult,
    OverdueScanRequest,
    OverdueScanResult,
    PolicyReason,
    RetrievalRequest,
    RetrievalResult,
    TrustPolicy,
)
from .policy import gate as _gate
from .retrieve import retrieve_beliefs
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

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        if self.policy is None:
            raise ValueError(
                "MemoryStore requires a policy to retrieve (pass policy=load_policy(...))"
            )
        return retrieve_beliefs(self._store, self.policy, self.agent_id, request)

    def assemble(self, request: AssemblyRequest) -> AssembledContext:
        if self.policy is None:
            raise ValueError(
                "MemoryStore requires a policy to assemble (pass policy=load_policy(...))"
            )
        return assemble_context(self._store, self.policy, self.agent_id, request)

    def gate(
        self,
        *,
        action: str,
        entity: str,
        scope: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> GateResult:
        if self.policy is None:
            raise ValueError("MemoryStore requires a policy to gate (pass policy=load_policy(...))")
        action_spec = self.policy.actions.get(action)
        if action_spec is None:
            return _gate(action, [], self.policy, {}, agent_id=self.agent_id)
        if scope is None:
            detail = PolicyReason(
                code="scope_context_missing",
                rule_id="SCOPE-CONTEXT-REQUIRED",
                message="active task scope is required for public gate evaluation",
            )
            return GateResult(
                decision=GateDecision.deny,
                action=action,
                decision_type=action_spec.decision,
                risk_tier=action_spec.risk,
                rule_ids=[detail.rule_id],
                reason_codes=[detail.code],
                reasons=[detail.message],
                details=[detail],
            )

        retrieval = self.retrieve(RetrievalRequest(
            entity=entity,
            attribute=action_spec.decision,
            scope=scope,
            task_type=task_type,
        ))
        supporting = [item.belief for item in retrieval.items]
        source_types = {item.source.id: item.source.type for item in retrieval.items}
        return _gate(action, supporting, self.policy, source_types, agent_id=self.agent_id)

    def correct(self, belief_id: int, *, reason: str):
        raise NotImplementedError("correct lands in M6")

    def explain(self, trace_id: int):
        raise NotImplementedError("explain lands in M7")

    def add_commitment(
        self, request: CommitmentCreateRequest
    ) -> CommitmentMutationResult:
        if self.policy is None:
            raise ValueError(
                "MemoryStore requires a policy to add commitments "
                "(pass policy=load_policy(...))"
            )
        return _add_commitment(self._store, self.policy, self.agent_id, request)

    def transition_commitment(
        self, request: CommitmentTransitionRequest
    ) -> CommitmentMutationResult:
        if self.policy is None:
            raise ValueError(
                "MemoryStore requires a policy to transition commitments "
                "(pass policy=load_policy(...))"
            )
        return _transition_commitment(
            self._store, self.policy, self.agent_id, request
        )

    def list_commitments(
        self, request: CommitmentListRequest
    ) -> CommitmentListResult:
        if self.policy is None:
            raise ValueError(
                "MemoryStore requires a policy to list commitments "
                "(pass policy=load_policy(...))"
            )
        return _list_commitments(self._store, self.policy, self.agent_id, request)

    def surface_overdue(
        self, request: OverdueScanRequest
    ) -> OverdueScanResult:
        if self.policy is None:
            raise ValueError(
                "MemoryStore requires a policy to scan overdue commitments "
                "(pass policy=load_policy(...))"
            )
        return _surface_overdue(self._store, self.policy, self.agent_id, request)
