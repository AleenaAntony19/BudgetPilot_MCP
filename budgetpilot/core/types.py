"""
BudgetPilot Core Type Contract System.
Phase A: Contract definitions, Enums, Dataclasses, and serialization protocols.

Strict Rule: This module and all budgetpilot.core modules must NOT import
ollama, mcp, or fastapi.
"""

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import time
from typing import Any, Dict, List, Optional, Union
import uuid


class RecoveryAction(str, Enum):
    """
    Candidate recovery actions evaluated on failure.
    Includes the novel 'expand_scope' action alongside CodeRescue baseline actions.
    """
    REFLECT = "reflect"
    REPLAN = "replan"
    EXPAND_SCOPE = "expand_scope"
    ESCALATE = "escalate"


class FailureClass(str, Enum):
    """
    Taxonomy of execution failures parsed from verification outputs.
    Phase F diagnostic classes:
    - syntax_or_lint: Syntax/linting failures during compilation or import
    - assertion_local: Assertion failures within touched target files
    - symbol_outside_touched: Name/Import/Attribute errors referencing unread outside symbols
    - regression_elsewhere: Test failures in untouched modules
    - repeat_signature: Exact repeat of previous failure signature
    - capability_gap: Missing system tools, missing packages, or capability limits
    - env_or_flaky: Timeouts, network drops, permission errors
    """
    # Phase F 7 Core Classes
    SYNTAX_OR_LINT = "syntax_or_lint"
    ASSERTION_LOCAL = "assertion_local"
    SYMBOL_OUTSIDE_TOUCHED = "symbol_outside_touched"
    REGRESSION_ELSEWHERE = "regression_elsewhere"
    REPEAT_SIGNATURE = "repeat_signature"
    CAPABILITY_GAP = "capability_gap"
    ENV_OR_FLAKY = "env_or_flaky"

    # Legacy / alias classes for backward compatibility
    SYNTAX_ERROR = "syntax_error"
    TEST_FAILURE = "test_failure"
    TIMEOUT = "timeout"
    IMPORT_ERROR = "import_error"
    TYPE_ERROR = "type_error"
    RUNTIME_ERROR = "runtime_error"
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"


class GateVerdictStatus(str, Enum):
    """
    Status of the execute gate interception check.
    """
    ALLOW = "allow"
    REWRITE = "rewrite"
    DENY = "deny"


@dataclass
class ToolCall:
    """
    Represents an intercepted or emitted tool execution request.
    """
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "call_id": self.call_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        return cls(
            tool_name=data["tool_name"],
            arguments=data.get("arguments", {}),
            call_id=data.get("call_id"),
            timestamp=data.get("timestamp", time.time()),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "ToolCall":
        return cls.from_dict(json.loads(json_str))


@dataclass
class GateVerdict:
    """
    Verdict emitted by the Execute Gate for an intercepted tool call.
    """
    status: GateVerdictStatus
    reason: str = ""
    rewritten_call: Optional[ToolCall] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Class-level shortcuts for status comparison
    ALLOW = GateVerdictStatus.ALLOW
    REWRITE = GateVerdictStatus.REWRITE
    DENY = GateVerdictStatus.DENY

    @classmethod
    def allow(cls, reason: str = "Allowed by execute gate", **metadata: Any) -> "GateVerdict":
        return cls(status=GateVerdictStatus.ALLOW, reason=reason, metadata=metadata)

    @classmethod
    def rewrite(
        cls,
        rewritten_call: ToolCall,
        reason: str = "Rewritten to minimum-viable execution path",
        **metadata: Any,
    ) -> "GateVerdict":
        return cls(
            status=GateVerdictStatus.REWRITE,
            reason=reason,
            rewritten_call=rewritten_call,
            metadata=metadata,
        )

    @classmethod
    def deny(cls, reason: str = "Denied by execute gate", **metadata: Any) -> "GateVerdict":
        return cls(status=GateVerdictStatus.DENY, reason=reason, metadata=metadata)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, GateVerdict):
            return (
                self.status == other.status
                and self.reason == other.reason
                and self.rewritten_call == other.rewritten_call
                and self.metadata == other.metadata
            )
        if isinstance(other, (GateVerdictStatus, str)):
            val = other.value if isinstance(other, GateVerdictStatus) else other
            return self.status.value == val
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "rewritten_call": self.rewritten_call.to_dict() if self.rewritten_call else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GateVerdict":
        status_val = data["status"]
        if isinstance(status_val, GateVerdictStatus):
            status = status_val
        else:
            status = GateVerdictStatus(str(status_val).lower())

        rewritten = None
        if data.get("rewritten_call"):
            rewritten = ToolCall.from_dict(data["rewritten_call"])

        return cls(
            status=status,
            reason=data.get("reason", ""),
            rewritten_call=rewritten,
            metadata=data.get("metadata", {}),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "GateVerdict":
        return cls.from_dict(json.loads(json_str))


@dataclass
class ScopePrediction:
    """
    Task complexity and scope prediction produced by the Estimate probe.
    Scope values: 1 (small / single-file), 2 (medium / multi-file local), 3 (large / cross-repo).
    """
    scope: int
    confidence: float = 1.0
    budget_tokens: int = 4000
    max_read_files: int = 3
    rationale: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "confidence": self.confidence,
            "budget_tokens": self.budget_tokens,
            "max_read_files": self.max_read_files,
            "rationale": self.rationale,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScopePrediction":
        return cls(
            scope=int(data["scope"]),
            confidence=float(data.get("confidence", 1.0)),
            budget_tokens=int(data.get("budget_tokens", 4000)),
            max_read_files=int(data.get("max_read_files", 3)),
            rationale=data.get("rationale", ""),
            metadata=data.get("metadata", {}),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "ScopePrediction":
        return cls.from_dict(json.loads(json_str))


@dataclass
class RecoveryContext:
    """
    Diagnostic context assembled on verification failure to feed the Recovery Router.
    """
    failure_class: FailureClass = FailureClass.UNKNOWN
    error_message: str = ""
    test_command: str = ""
    stderr: str = ""
    attempt_number: int = 1
    files_touched: List[str] = field(default_factory=list)
    previous_actions: List[RecoveryAction] = field(default_factory=list)
    tokens_spent: int = 0
    cost_spent_usd: float = 0.0
    session_id: str = ""
    prompt: str = ""
    diff_size: int = 0
    outside_frames: List[str] = field(default_factory=list)
    unread_symbols: List[str] = field(default_factory=list)
    regressions: List[str] = field(default_factory=list)
    signature_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "error_message": self.error_message,
            "test_command": self.test_command,
            "stderr": self.stderr,
            "attempt_number": self.attempt_number,
            "files_touched": list(self.files_touched),
            "previous_actions": [a.value for a in self.previous_actions],
            "tokens_spent": self.tokens_spent,
            "cost_spent_usd": self.cost_spent_usd,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "diff_size": self.diff_size,
            "outside_frames": list(self.outside_frames),
            "unread_symbols": list(self.unread_symbols),
            "regressions": list(self.regressions),
            "signature_hash": self.signature_hash,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecoveryContext":
        fc = data["failure_class"]
        failure_class = fc if isinstance(fc, FailureClass) else FailureClass(str(fc).lower())

        raw_prev = data.get("previous_actions", [])
        previous_actions = [
            a if isinstance(a, RecoveryAction) else RecoveryAction(str(a).lower())
            for a in raw_prev
        ]

        return cls(
            failure_class=failure_class,
            error_message=data.get("error_message", ""),
            test_command=data.get("test_command", ""),
            stderr=data.get("stderr", ""),
            attempt_number=int(data.get("attempt_number", 1)),
            files_touched=list(data.get("files_touched", [])),
            previous_actions=previous_actions,
            tokens_spent=int(data.get("tokens_spent", 0)),
            cost_spent_usd=float(data.get("cost_spent_usd", 0.0)),
            session_id=data.get("session_id", ""),
            prompt=data.get("prompt", ""),
            diff_size=int(data.get("diff_size", 0)),
            outside_frames=list(data.get("outside_frames", [])),
            unread_symbols=list(data.get("unread_symbols", [])),
            regressions=list(data.get("regressions", [])),
            signature_hash=data.get("signature_hash"),
            metadata=data.get("metadata", {}),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "RecoveryContext":
        return cls.from_dict(json.loads(json_str))


@dataclass
class RouterDecision:
    """
    Action recommendation produced by the cost-calibrated Recovery Router.
    """
    action: RecoveryAction
    target_tier: str = "cheap"
    cost_penalty_lambda: float = 0.0
    confidence: float = 1.0
    rationale: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "target_tier": self.target_tier,
            "cost_penalty_lambda": self.cost_penalty_lambda,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RouterDecision":
        act = data["action"]
        action = act if isinstance(act, RecoveryAction) else RecoveryAction(str(act).lower())

        return cls(
            action=action,
            target_tier=data.get("target_tier", "cheap"),
            cost_penalty_lambda=float(data.get("cost_penalty_lambda", 0.0)),
            confidence=float(data.get("confidence", 1.0)),
            rationale=data.get("rationale", ""),
            metadata=data.get("metadata", {}),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "RouterDecision":
        return cls.from_dict(json.loads(json_str))


@dataclass
class BpEvent:
    """
    Telemetry and audit event recorded to the ledger.
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    event_type: str = "telemetry"
    session_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "payload": self.payload,
            "cost_usd": self.cost_usd,
            "latency_seconds": self.latency_seconds,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BpEvent":
        return cls(
            event_id=data.get("event_id", str(uuid.uuid4())),
            timestamp=float(data.get("timestamp", time.time())),
            event_type=data.get("event_type", "telemetry"),
            session_id=data.get("session_id", ""),
            payload=data.get("payload", {}),
            cost_usd=float(data.get("cost_usd", 0.0)),
            latency_seconds=float(data.get("latency_seconds", 0.0)),
            metadata=data.get("metadata", {}),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "BpEvent":
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def from_tool_call(
        cls,
        tool_call: Union["ToolCall", Dict[str, Any]],
        session_id: str = "",
        cost_usd: float = 0.0,
        latency_seconds: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "BpEvent":
        return normalize_tool_call(
            tool_call=tool_call,
            session_id=session_id,
            cost_usd=cost_usd,
            latency_seconds=latency_seconds,
            metadata=metadata,
        )


CANONICAL_TOOL_TYPES = ("list_dir", "search", "read_file", "edit_file", "run_tests", "request_scope")


def normalize_tool_call(
    tool_call: Union[ToolCall, Dict[str, Any]],
    session_id: str = "",
    cost_usd: float = 0.0,
    latency_seconds: float = 0.0,
    metadata: Optional[Dict[str, Any]] = None,
) -> BpEvent:
    """
    Normalize any model tool call payload into a standard BpEvent record.
    Canonical event types: 'list_dir', 'search', 'read_file', 'edit_file', 'run_tests', 'other'.
    """
    raw_name = ""
    raw_args: Any = {}
    call_id: Optional[str] = None

    if isinstance(tool_call, ToolCall):
        raw_name = tool_call.tool_name
        raw_args = tool_call.arguments
        call_id = tool_call.call_id
    elif isinstance(tool_call, dict):
        call_id = tool_call.get("id") or tool_call.get("call_id")

        if "function" in tool_call and isinstance(tool_call["function"], dict):
            fn = tool_call["function"]
            raw_name = fn.get("name", "")
            raw_args = fn.get("arguments", {})
        elif "tool_name" in tool_call:
            raw_name = tool_call.get("tool_name", "")
            raw_args = tool_call.get("arguments", tool_call.get("args", {}))
        elif "name" in tool_call:
            raw_name = tool_call.get("name", "")
            raw_args = tool_call.get("arguments", tool_call.get("args", {}))
        elif "tool" in tool_call:
            raw_name = tool_call.get("tool", "")
            raw_args = tool_call.get("args", tool_call.get("arguments", {}))
        else:
            raw_name = "unknown"
            raw_args = tool_call
    else:
        # Object-based tool call (e.g. pydantic model, ollama ToolCall, custom object)
        if hasattr(tool_call, "model_dump"):
            d = tool_call.model_dump()
            return normalize_tool_call(d, session_id=session_id, cost_usd=cost_usd, latency_seconds=latency_seconds, metadata=metadata)
        elif hasattr(tool_call, "dict"):
            d = tool_call.dict()
            return normalize_tool_call(d, session_id=session_id, cost_usd=cost_usd, latency_seconds=latency_seconds, metadata=metadata)

        call_id = getattr(tool_call, "id", None) or getattr(tool_call, "call_id", None)
        fn = getattr(tool_call, "function", None)
        if fn is not None:
            if isinstance(fn, dict):
                raw_name = fn.get("name", "")
                raw_args = fn.get("arguments", {})
            else:
                raw_name = getattr(fn, "name", "")
                raw_args = getattr(fn, "arguments", {})
        elif hasattr(tool_call, "tool_name"):
            raw_name = getattr(tool_call, "tool_name", "")
            raw_args = getattr(tool_call, "arguments", {})
        elif hasattr(tool_call, "name"):
            raw_name = getattr(tool_call, "name", "")
            raw_args = getattr(tool_call, "arguments", {})
        else:
            raw_name = "unknown"
            raw_args = str(tool_call)

    # Safely parse JSON string arguments if emitted as string
    if isinstance(raw_args, str):
        try:
            parsed_args = json.loads(raw_args)
        except Exception:
            parsed_args = {"raw": raw_args}
    elif isinstance(raw_args, dict):
        parsed_args = raw_args
    else:
        parsed_args = {"raw": raw_args}

    tool_name_clean = str(raw_name).strip().lower()
    event_type = tool_name_clean if tool_name_clean in CANONICAL_TOOL_TYPES else "other"

    payload = {
        "tool_name": tool_name_clean,
        "arguments": parsed_args,
        "call_id": call_id,
    }

    event_metadata = dict(metadata or {})
    event_metadata["original_tool_name"] = raw_name

    return BpEvent(
        event_type=event_type,
        session_id=session_id,
        payload=payload,
        cost_usd=cost_usd,
        latency_seconds=latency_seconds,
        metadata=event_metadata,
    )

