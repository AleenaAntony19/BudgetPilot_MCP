"""
BudgetPilot Safety, Hardening & Security Subsystem.
Phase O: Global kill switch enforcement, automated secret redaction,
hard per-task cap guards, and network egress safety auditing.

Strict Rule: This module must NOT import ollama, mcp, fastapi, or uvicorn.
"""

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import socket
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ===========================================================================
# 1. Global Kill Switch (BP_MODE=off)
# ===========================================================================

def is_kill_switch_active() -> bool:
    """
    Check if the global emergency kill switch (BP_MODE=off) is active.
    When active, all interception, gating, budget enforcement, and recovery
    are immediately bypassed, operating in fail-open passthrough mode.
    """
    env_mode = os.environ.get("BP_MODE", "").strip().lower()
    if env_mode == "off":
        return True

    # Check config file if env not explicitly set
    try:
        from budgetpilot.config import ExecutionMode, load_config
        cfg = load_config()
        return cfg.mode == ExecutionMode.OFF
    except Exception:
        return False


def set_kill_switch(active: bool = True) -> None:
    """
    Programmatically activate or deactivate the global emergency kill switch.
    """
    if active:
        os.environ["BP_MODE"] = "off"
    else:
        if os.environ.get("BP_MODE") == "off":
            del os.environ["BP_MODE"]


def enforce_kill_switch() -> bool:
    """
    Enforce global kill switch check.
    Returns True if execution should bypass all BudgetPilot gates and limits.
    """
    return is_kill_switch_active()


# ===========================================================================
# 2. Automated Secret Redaction Engine
# ===========================================================================

# High-precision regular expression patterns for credential and secret detection
SECRET_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    # AWS Access Key ID
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # AWS Secret Access Key assignment
    (
        "AWS_SECRET",
        re.compile(r"(?i)(aws_secret_access_key|aws_secret|secret_key|secret_token)\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
        r"\1=[REDACTED_AWS_SECRET]",
    ),
    # GitHub Personal Access Tokens (classic and fine-grained)
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,255}\b"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    # Bearer Authentication Tokens
    (
        "BEARER_TOKEN",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]{15,}\b"),
        "Bearer [REDACTED_BEARER_TOKEN]",
    ),
    # Slack API Tokens (including safe demo/test examples)
    (
        "SLACK_TOKEN",
        re.compile(
            r"\b(?:xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,32}|slack(?:[_-]?demo)?(?:[_-]?token)?[_-][A-Za-z0-9]{10,})\b"
        ),
        "[REDACTED_SLACK_TOKEN]",
    ),
    # Asymmetric Private Keys (RSA, OpenSSH, EC, PGP)
    (
        "PRIVATE_KEY",
        re.compile(r"-----BEGIN (?:[A-Z0-9_\-]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9_\-]+ )?PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Generic API Keys / Passwords / Secrets in assignment format
    (
        "GENERIC_SECRET",
        re.compile(r"(?i)(api[_-]?key|auth[_-]?token|access[_-]?token|password)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{16,})['\"]?"),
        r"\1=[REDACTED_SECRET]",
    ),
    # JSON Web Tokens (JWT)
    (
        "JWT_TOKEN",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        "[REDACTED_JWT]",
    ),
]


def redact_secrets(text: Union[str, Any]) -> str:
    """
    Sanitize text by replacing sensitive secrets, tokens, AWS keys,
    bearer tokens, and private keys with explicit redacted placeholders.
    """
    if not isinstance(text, str):
        text = str(text)

    sanitized = text
    for name, pattern, replacement in SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)

    return sanitized


def is_secret_present(text: str) -> bool:
    """
    Check if any known secret or credential pattern is present in the text.
    """
    if not isinstance(text, str) or not text:
        return False

    for name, pattern, _ in SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False


def redact_data(obj: Any) -> Any:
    """
    Recursively sanitize dictionaries, lists, tuples, and strings,
    redacting any embedded secrets prior to SQLite persistence or logging.
    """
    if isinstance(obj, str):
        return redact_secrets(obj)
    elif isinstance(obj, dict):
        return {k: redact_data(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [redact_data(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(redact_data(item) for item in obj)
    elif isinstance(obj, set):
        return {redact_data(item) for item in obj}
    elif hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        try:
            return redact_data(obj.to_dict())
        except Exception:
            return redact_secrets(repr(obj))
    elif hasattr(obj, "__dict__"):
        try:
            return {k: redact_data(v) for k, v in vars(obj).items() if not k.startswith("_")}
        except Exception:
            return redact_secrets(repr(obj))
    else:
        return obj


# ===========================================================================
# 3. Hard Per-Task Cap Guards
# ===========================================================================

class CapViolationError(RuntimeError):
    """
    Raised when a hard execution cap (turns, denials, recovery attempts, or cost)
    is breached during an agent session.
    """
    def __init__(self, cap_name: str, limit: Union[int, float], current: Union[int, float]):
        self.cap_name = cap_name
        self.limit = limit
        self.current = current
        super().__init__(
            f"Hard task cap violated: '{cap_name}' exceeded limit {limit} (current: {current}). "
            f"Session execution halted for budget safety."
        )


@dataclass
class CapCheckResult:
    """Result emitted by task cap evaluations."""
    allowed: bool
    cap_name: Optional[str] = None
    limit: Optional[Union[int, float]] = None
    current: Optional[Union[int, float]] = None
    reason: str = "All task caps within safe limits"


@dataclass
class TaskCapGuard:
    """
    Enforces hard safety caps per task session:
    - max_denials: Maximum gate denials before switching or aborting (default 5).
    - max_recovery_attempts: Maximum recovery attempts (reflect/replan/escalate) (default 3).
    - max_turns: Maximum multi-turn agent turns (default 25).
    - max_escalation_usd: Maximum dollar spend allocated to strong-tier escalation (default $1.00).
    """
    max_denials: int = 5
    max_recovery_attempts: int = 3
    max_turns: int = 25
    max_escalation_usd: float = 1.00

    def check_denials(self, denial_count: int, raise_on_violation: bool = True) -> CapCheckResult:
        """Verify denial count against hard cap."""
        if denial_count > self.max_denials:
            if raise_on_violation:
                raise CapViolationError("max_denials", self.max_denials, denial_count)
            return CapCheckResult(
                allowed=False,
                cap_name="max_denials",
                limit=self.max_denials,
                current=denial_count,
                reason=f"Exceeded max denials cap ({denial_count} > {self.max_denials})",
            )
        return CapCheckResult(allowed=True)

    def check_recovery_attempts(self, attempts: int, raise_on_violation: bool = True) -> CapCheckResult:
        """Verify recovery attempt count against hard cap."""
        if attempts > self.max_recovery_attempts:
            if raise_on_violation:
                raise CapViolationError("max_recovery_attempts", self.max_recovery_attempts, attempts)
            return CapCheckResult(
                allowed=False,
                cap_name="max_recovery_attempts",
                limit=self.max_recovery_attempts,
                current=attempts,
                reason=f"Exceeded max recovery attempts cap ({attempts} > {self.max_recovery_attempts})",
            )
        return CapCheckResult(allowed=True)

    def check_turns(self, turns_used: int, raise_on_violation: bool = True) -> CapCheckResult:
        """Verify agent loop turn count against hard cap."""
        if turns_used > self.max_turns:
            if raise_on_violation:
                raise CapViolationError("max_turns", self.max_turns, turns_used)
            return CapCheckResult(
                allowed=False,
                cap_name="max_turns",
                limit=self.max_turns,
                current=turns_used,
                reason=f"Exceeded max turns cap ({turns_used} > {self.max_turns})",
            )
        return CapCheckResult(allowed=True)

    def check_escalation_cost(self, cost_usd: float, raise_on_violation: bool = True) -> CapCheckResult:
        """Verify strong-tier escalation expenditure against hard cap."""
        if cost_usd > self.max_escalation_usd:
            if raise_on_violation:
                raise CapViolationError("max_escalation_usd", self.max_escalation_usd, round(cost_usd, 4))
            return CapCheckResult(
                allowed=False,
                cap_name="max_escalation_usd",
                limit=self.max_escalation_usd,
                current=round(cost_usd, 4),
                reason=f"Exceeded max escalation USD cap (${cost_usd:.4f} > ${self.max_escalation_usd:.2f})",
            )
        return CapCheckResult(allowed=True)

    def evaluate_task_state(
        self,
        denials: int = 0,
        recovery_attempts: int = 0,
        turns: int = 0,
        escalation_cost_usd: float = 0.0,
        raise_on_violation: bool = False,
    ) -> CapCheckResult:
        """
        Evaluate all hard caps against current task state counters.
        """
        res = self.check_denials(denials, raise_on_violation=raise_on_violation)
        if not res.allowed:
            return res

        res = self.check_recovery_attempts(recovery_attempts, raise_on_violation=raise_on_violation)
        if not res.allowed:
            return res

        res = self.check_turns(turns, raise_on_violation=raise_on_violation)
        if not res.allowed:
            return res

        res = self.check_escalation_cost(escalation_cost_usd, raise_on_violation=raise_on_violation)
        if not res.allowed:
            return res

        return CapCheckResult(allowed=True)


# Default global cap guard instance
DEFAULT_CAP_GUARD = TaskCapGuard()


# ===========================================================================
# 4. Network Egress Safety & Auditing
# ===========================================================================

# Standard loopback and local address definitions
LOCAL_HOSTS: Set[str] = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "[::1]",
}


def get_allowed_ollama_host() -> str:
    """Resolve configured or default Ollama host name."""
    ollama_host_env = os.environ.get("OLLAMA_HOST", "")
    if ollama_host_env:
        # Can be http://host:port or host:port or host
        if "://" not in ollama_host_env:
            ollama_host_env = "http://" + ollama_host_env
        parsed = urlparse(ollama_host_env)
        return parsed.hostname or "localhost"
    return "localhost"


def is_allowed_network_target(host: str, port: Optional[int] = None) -> bool:
    """
    Verify whether a target network host is an authorized local Ollama endpoint.
    BudgetPilot is local-first: ALL outbound connections to third-party or external
    IPs/domains are strictly unauthorized.
    """
    if not host:
        return False

    clean_host = str(host).strip().lower()

    # 1. Direct match with local loopback definitions or 127.x.x.x
    if clean_host in LOCAL_HOSTS or clean_host.startswith("127."):
        return True

    # Strip port if present in hostname string
    if clean_host.startswith("[") and "]" in clean_host:
        clean_host = clean_host[1:].split("]")[0]
    elif clean_host.count(":") == 1:
        # Standard IPv4 or hostname with port (e.g., 127.0.0.1:11434 or localhost:8080)
        clean_host = clean_host.split(":")[0]

    # Re-check local loopback definitions after port stripping
    if clean_host in LOCAL_HOSTS or clean_host.startswith("127."):
        return True

    # 2. Check explicitly configured OLLAMA_HOST
    allowed_ollama = get_allowed_ollama_host().lower()
    if clean_host == allowed_ollama:
        return True

    return False


def assert_network_safety(target_host: str, target_port: Optional[int] = None) -> None:
    """
    Assert that the network target is permitted under BudgetPilot's local-only policy.
    Raises PermissionError if an unauthorized outbound external connection is attempted.
    """
    if not is_allowed_network_target(target_host, target_port):
        raise PermissionError(
            f"Unauthorized external network connection blocked by BudgetPilot Security Policy: '{target_host}'. "
            f"BudgetPilot strictly permits network connections ONLY to the local Ollama host."
        )


class NetworkAuditContext:
    """
    Context manager that intercepts raw socket connection attempts to enforce
    that the only external connections made are to the authorized local Ollama host.
    """
    def __init__(self, allowed_hosts: Optional[Set[str]] = None):
        self.allowed_hosts = set(LOCAL_HOSTS)
        if allowed_hosts:
            self.allowed_hosts.update(h.lower() for h in allowed_hosts)
        self.allowed_hosts.add(get_allowed_ollama_host().lower())
        self.connections_attempted: List[Tuple[str, int]] = []
        self._orig_connect = None

    def __enter__(self):
        self._orig_connect = socket.socket.connect
        audit_self = self

        def audited_connect(sock_self, address):
            if isinstance(address, tuple) and len(address) >= 2:
                host, port = address[0], address[1]
                audit_self.connections_attempted.append((str(host), int(port)))
                clean_host = str(host).strip().lower()
                is_local = (
                    is_allowed_network_target(clean_host, port)
                    or clean_host in audit_self.allowed_hosts
                )
                if not is_local:
                    raise PermissionError(
                        f"BudgetPilot Network Audit blocked connection to external host: {host}:{port}"
                    )
            return audit_self._orig_connect(sock_self, address)

        socket.socket.connect = audited_connect
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._orig_connect:
            socket.socket.connect = self._orig_connect
