"""Official MCP stdio adapter for the governed ``MemoryStore`` service.

Trusted process configuration supplies identity, persistence, and session mode.
Tool arguments contain domain inputs only.  The adapter deliberately uses the
official SDK's public low-level ``Server`` API because its schema validator can
reject unknown top-level arguments; stable FastMCP 1.x currently ignores them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import mcp.server.stdio
import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from .core import Clock, IdFactory, MemoryStore
from .models import (
    AssemblyRequest,
    CandidateBelief,
    CorrectionRequest,
    ExplainRequest,
    RetrievalRequest,
    Scope,
    SessionMode,
    TrustPolicy,
)
from .policy import load_policy


LOGGER = logging.getLogger("epistemic_memory.mcp")
SERVER_NAME = "epistemic-memory"
SERVER_VERSION = "0.1.0"
TOOL_NAMES = (
    "memory_ingest",
    "memory_retrieve",
    "memory_assemble_context",
    "memory_gate_action",
    "memory_explain",
    "memory_correct",
)


@dataclass(frozen=True)
class HostConfig:
    """Trusted, process-startup-only MCP host configuration."""

    database_path: str
    policy_path: str
    agent_id: str
    session_mode: SessionMode = SessionMode.direct
    session_id: Optional[str] = None
    approval_actor_id: Optional[str] = None
    live_extraction: bool = False

    def __post_init__(self) -> None:
        for name in ("database_path", "policy_path", "agent_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        for name in ("session_id", "approval_actor_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must not be empty")
        object.__setattr__(self, "session_mode", SessionMode(self.session_mode))


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestToolInput(_ToolInput):
    source_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    scope: str
    meta: Optional[dict[str, JsonValue]] = None
    candidates: Optional[list[CandidateBelief]] = None

    @field_validator("source_id", "content")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return Scope.parse(value).render()


class GateToolInput(_ToolInput):
    action: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    scope: str
    task_type: Optional[str] = None

    @field_validator("action", "entity")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        return Scope.parse(value).render()

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not value.strip() or ":" in value:
            raise ValueError("task_type must be a non-empty bare task name")
        return value


class ToolEnvelope(_ToolInput):
    tool: Literal[
        "memory_ingest",
        "memory_retrieve",
        "memory_assemble_context",
        "memory_gate_action",
        "memory_explain",
        "memory_correct",
    ]
    ok: bool
    result_code: Optional[str] = None
    trace_id: Optional[str] = None
    data: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass
class SessionState:
    memory: MemoryStore
    config: HostConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fault_sequence: int = 0

    def next_fault_id(self) -> str:
        self.fault_sequence += 1
        return f"mcp-internal-{self.fault_sequence:04d}"


MemoryFactory = Callable[..., MemoryStore]


def _model_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema(by_alias=True, mode="validation")


def _description(tool: str) -> str:
    descriptions = {
        "memory_ingest": (
            "Ingests an event through MemoryStore. Direct mode persists an event, beliefs, "
            "and audit trace; propose mode persists proposals instead of beliefs; ephemeral "
            "mode fails closed. Agent identity and mode are host-controlled, and source "
            "attribution requires an exact policy allowlist. Returns the full typed "
            "ingest/proposal result and trace ID."
        ),
        "memory_retrieve": (
            "Performs governed raw retrieval through MemoryStore with mandatory task scope. "
            "It does not create an audit trace. Agent identity and session mode are "
            "host-controlled; unauthorized scope fails closed. Returns ranked beliefs, "
            "exclusions, and deduplication decisions."
        ),
        "memory_assemble_context": (
            "Assembles governed context through MemoryStore with mandatory task scope. It "
            "persists an audit trace in durable modes and creates a same-session transient "
            "trace in ephemeral mode. Host controls authority and mode. Returns rendered "
            "context, receipt, permissions, token metadata, and trace ID."
        ),
        "memory_gate_action": (
            "Evaluates a policy-derived action gate through MemoryStore with mandatory task "
            "scope; callers cannot choose the decision type. It records a durable or transient "
            "audit trace; the host controls authority and mode. It fails closed on unsafe "
            "evidence and returns the typed decision, reasons, rules, and trace ID."
        ),
        "memory_explain": (
            "Explains a persisted or same-session transient trace through MemoryStore with "
            "mandatory task scope and an optional belief-removal counterfactual. It is "
            "read-only, host-authorized, and fails closed when the trace or scope is "
            "unavailable. Returns historical evidence, counterfactual, follow-up, and rendering."
        ),
        "memory_correct": (
            "Applies an authorized same-source correction or retraction through MemoryStore "
            "with mandatory task scope. Provenance identity and session mode are host-controlled; "
            "ephemeral or unauthorized writes fail closed. Returns the typed correction, "
            "propagation impacts, hidden-impact summary, and trace ID."
        ),
    }
    return descriptions[tool]


def _tool_definitions() -> list[mcp_types.Tool]:
    inputs: dict[str, type[BaseModel]] = {
        "memory_ingest": IngestToolInput,
        "memory_retrieve": RetrievalRequest,
        "memory_assemble_context": AssemblyRequest,
        "memory_gate_action": GateToolInput,
        "memory_explain": ExplainRequest,
        "memory_correct": CorrectionRequest,
    }
    read_only = {"memory_retrieve", "memory_explain"}
    output_schema = _model_schema(ToolEnvelope)
    return [
        mcp_types.Tool(
            name=name,
            description=_description(name),
            inputSchema=_model_schema(inputs[name]),
            outputSchema=output_schema,
            annotations=(
                mcp_types.ToolAnnotations(readOnlyHint=True)
                if name in read_only
                else None
            ),
        )
        for name in TOOL_NAMES
    ]


def _json_object(value: BaseModel) -> dict[str, JsonValue]:
    data = value.model_dump(mode="json")
    encoded = json.dumps(
        data,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return json.loads(encoded)


def _code(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _envelope(
    tool: str,
    result: BaseModel,
    *,
    ok: bool,
    result_code: object = None,
    trace_id: Optional[str] = None,
) -> dict[str, JsonValue]:
    return _json_object(ToolEnvelope(
        tool=tool,
        ok=ok,
        result_code=_code(result_code),
        trace_id=trace_id,
        data=_json_object(result),
    ))


def _boundary_failure(tool: str, code: str) -> dict[str, JsonValue]:
    return _json_object(ToolEnvelope(
        tool=tool,
        ok=False,
        result_code=code,
        data={},
    ))


def _extractor(candidates: list[CandidateBelief]):
    frozen = [candidate.model_copy(deep=True) for candidate in candidates]

    def extract(_event, _source_type) -> list[CandidateBelief]:
        return [candidate.model_copy(deep=True) for candidate in frozen]

    return extract


def _call_domain(state: SessionState, tool: str, arguments: dict[str, Any]):
    memory = state.memory
    if tool == "memory_ingest":
        request = IngestToolInput.model_validate(arguments)
        if request.candidates is None and not state.config.live_extraction:
            return _boundary_failure(tool, "validation_error")
        result = memory.ingest(
            source_id=request.source_id,
            content=request.content,
            scope=request.scope,
            meta=request.meta,
            extractor=(
                _extractor(request.candidates)
                if request.candidates is not None
                else None
            ),
        )
        return _envelope(
            tool,
            result,
            ok=result.ok,
            result_code=result.code,
            trace_id=result.trace_id,
        )
    if tool == "memory_retrieve":
        result = memory.retrieve(RetrievalRequest.model_validate(arguments))
        return _envelope(tool, result, ok=result.authorized)
    if tool == "memory_assemble_context":
        result = memory.assemble(AssemblyRequest.model_validate(arguments))
        return _envelope(
            tool,
            result,
            ok=result.ok,
            result_code=result.result_code,
            trace_id=result.trace_id,
        )
    if tool == "memory_gate_action":
        request = GateToolInput.model_validate(arguments)
        result = memory.gate(
            action=request.action,
            entity=request.entity,
            scope=request.scope,
            task_type=request.task_type,
        )
        return _envelope(
            tool,
            result,
            ok=result.ok,
            result_code=result.result_code,
            trace_id=result.trace_id,
        )
    if tool == "memory_explain":
        result = memory.explain(ExplainRequest.model_validate(arguments))
        return _envelope(
            tool,
            result,
            ok=result.authorized,
            result_code=result.code,
            trace_id=result.trace_id,
        )
    if tool == "memory_correct":
        result = memory.correct(CorrectionRequest.model_validate(arguments))
        return _envelope(
            tool,
            result,
            ok=result.ok,
            result_code=result.code,
            trace_id=result.trace_id,
        )
    return _boundary_failure(tool, "unknown_tool")


def create_server(
    config: HostConfig,
    *,
    policy: Optional[TrustPolicy] = None,
    memory_factory: MemoryFactory = MemoryStore,
    clock: Optional[Clock] = None,
    id_factory: Optional[IdFactory] = None,
) -> Server[SessionState]:
    """Build an inert server; the single store opens only inside lifespan."""

    @asynccontextmanager
    async def lifespan(_server: Server[SessionState]) -> AsyncIterator[SessionState]:
        effective_policy = policy or load_policy(config.policy_path)
        if config.agent_id not in effective_policy.agents:
            raise ValueError("configured agent_id is not present in the trust policy")
        memory = memory_factory(
            config.database_path,
            effective_policy,
            agent_id=config.agent_id,
            session_mode=config.session_mode,
            session_id=config.session_id,
            approval_actor_id=config.approval_actor_id,
            live=config.live_extraction,
            clock=clock,
            id_factory=id_factory,
        )
        state = SessionState(memory=memory, config=config)
        try:
            yield state
        finally:
            memory.close()

    server: Server[SessionState] = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "Governed epistemic memory tools. Identity, policy, persistence, and "
            "session mode are controlled by the trusted host process."
        ),
        lifespan=lifespan,
    )

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return _tool_definitions()

    @server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict[str, Any]):
        state: Optional[SessionState] = None
        try:
            state = server.request_context.lifespan_context
            async with state.lock:
                return _call_domain(state, name, arguments)
        except ValidationError:
            return _boundary_failure(name, "validation_error")
        except ValueError:
            return _boundary_failure(name, "domain_rejected")
        except Exception:
            correlation_id = (
                state.next_fault_id() if state is not None else "mcp-internal-startup"
            )
            LOGGER.exception("MCP tool internal fault [%s]", correlation_id)
            return _boundary_failure(name, "internal_error")

    return server


async def run_server(server: Server[SessionState]) -> None:
    """Run one official MCP stdio session until EOF or cancellation."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epistemic-memory-mcp",
        description="Run the Epistemic Memory M8 MCP server over stdio.",
    )
    parser.add_argument("--db", required=True, metavar="PATH")
    parser.add_argument("--policy", required=True, metavar="PATH")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument(
        "--session-mode",
        choices=[mode.value for mode in SessionMode],
        default=SessionMode.direct.value,
    )
    parser.add_argument("--session-id")
    parser.add_argument("--approval-actor-id")
    parser.add_argument("--live-extraction", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> HostConfig:
    if args.live_extraction and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("live extraction requires ANTHROPIC_API_KEY")
    policy_path = Path(args.policy).expanduser()
    if not policy_path.is_file():
        raise ValueError("policy file does not exist")
    if args.session_mode == SessionMode.ephemeral.value:
        db_path = Path(args.db).expanduser()
        if not db_path.is_file():
            raise ValueError("ephemeral mode requires an existing database file")
    return HostConfig(
        database_path=args.db,
        policy_path=args.policy,
        agent_id=args.agent_id,
        session_mode=SessionMode(args.session_mode),
        session_id=args.session_id,
        approval_actor_id=args.approval_actor_id,
        live_extraction=args.live_extraction,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Console/module entry point. Stdout remains reserved for MCP traffic."""
    parser = _parser()
    try:
        config = _config_from_args(parser.parse_args(argv))
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        asyncio.run(run_server(create_server(config)))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"epistemic-memory-mcp: startup failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
