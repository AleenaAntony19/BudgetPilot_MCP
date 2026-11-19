"""
Billing detection: is this session billed per token, through a cloud account,
or against a subscription plan?

Presence only. No secret value is ever read into a string that leaves this
module: evidence lists variable names (and the normalised value of on/off
flags, which are not secrets), never a key, a token or a URL.

Claude Code signals, with how each was confirmed (2026-11-19, Claude Code
2.1.328 and https://code.claude.com/docs/en/env-vars, /hooks, /settings):

  CLAUDE_CODE_USE_BEDROCK, _VERTEX, _FOUNDRY      docs + `claude auth status` reports apiProvider
                                                   bedrock / vertex / foundry
  CLAUDE_CODE_USE_MANTLE                          docs (Bedrock Mantle endpoint) [VERIFY with a session]
  CLAUDE_CODE_USE_ANTHROPIC_AWS, _GOOGLE_CLOUD    docs (Anthropic-managed, billed through the cloud
                                                   account) [VERIFY billing and prices]
  ANTHROPIC_API_KEY                               docs: "used instead of your ... subscription even if
                                                   you are logged in"; always in -p, after a one-time
                                                   approval in interactive mode. Probed: -p used it;
                                                   an inherited claude-vscode entrypoint ignored it.
                                                   So: api (high) when headless, else unknown.
                                                   `claude auth status` does not reflect it.
  ANTHROPIC_AUTH_TOKEN                            docs (Bearer header); auth status: authMethod oauth_token
  ANTHROPIC_BASE_URL                              docs (proxy or gateway): who bills depends on the gateway
  apiKeyHelper in a settings file                 docs (credential helper)
  plan login                                      `claude auth status --json`: authMethod "claude.ai",
                                                   subscriptionType set. 170 ms, so never run from a hook:
                                                   `bp doctor` and `bp install` cache the safe fields.
  CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1, HIPAA mode  docs: credentials are removed from the hook's
                                                   environment, so absence of a key proves nothing.

Precedence: user override > cloud flag > API key / auth token / key helper /
base URL > confirmed plan login > unknown. `unknown` is displayed like a
subscription (tokens, no dollars).

Standard library only; safe to import from the hook process.
"""

from __future__ import annotations

import json
import os
import time

from budgetpilot.hosts.base import BILLING_MODES, BillingContext

TRUE_VALUES = ("1", "true", "yes", "on")

# flag -> (provider, label). Provider names follow BillingContext's vocabulary.
CLOUD_FLAGS = (
    ("CLAUDE_CODE_USE_BEDROCK", "bedrock", "Amazon Bedrock"),
    ("CLAUDE_CODE_USE_MANTLE", "bedrock", "Amazon Bedrock (Mantle endpoint)"),
    ("CLAUDE_CODE_USE_VERTEX", "vertex", "Google Cloud Agent Platform (Vertex)"),
    ("CLAUDE_CODE_USE_FOUNDRY", "azure", "Microsoft Foundry"),
    ("CLAUDE_CODE_USE_ANTHROPIC_AWS", "anthropic", "Claude Platform on AWS"),
    ("CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD", "anthropic", "Claude on Google Cloud (Anthropic managed)"),
)
# Variables that make Claude Code bill per token instead of the plan. These are
# what the key-set warning looks for.
KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
PROVIDERS = ("anthropic", "openai", "google", "bedrock", "vertex", "azure", "other", "unknown")
AUTH_CACHE_FIELDS = ("loggedIn", "authMethod", "apiProvider", "subscriptionType")
AUTH_CACHE_FILE = "claude_auth.json"
AUTH_CACHE_FRESH_S = 7 * 86400


def _flag(env, name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in TRUE_VALUES


def _present(env, name: str) -> bool:
    return bool(str(env.get(name, "")).strip())


def _is_local_url(value: str) -> bool:
    """True when a base URL points at this machine. The URL itself is never stored."""
    rest = value.split("://", 1)[-1]
    host = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    else:
        host = host.split(":", 1)[0]
    return host.lower() in ("localhost", "127.0.0.1", "::1")


# ------------------------------------------------------------------ auth status cache
def _home():
    from budgetpilot.core.state import get_bp_home

    return get_bp_home()


def read_auth_cache(path=None):
    """The safe fields of the last `claude auth status --json`, or None."""
    try:
        data = json.loads((path or (_home() / AUTH_CACHE_FILE)).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_auth_cache(status: dict, claude_version: str = "", path=None) -> dict:
    """Keep only the whitelisted, non-personal fields (no email, organisation or expiry)."""
    safe = {k: status.get(k) for k in AUTH_CACHE_FIELDS if k in status}
    safe["checked_at"] = time.time()
    safe["claude_version"] = str(claude_version or "")[:40]
    target = path or (_home() / AUTH_CACHE_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(safe, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return safe


def refresh_auth_cache(binary: str, timeout: float = 20.0):
    """Run `claude auth status --json` (CLI only, never from a hook) and cache the safe fields."""
    import subprocess

    try:
        out = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True, timeout=timeout)
        status = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(status, dict):
        return None
    version = ""
    try:
        from budgetpilot.cc_cli import claude_version

        version = claude_version(binary)
    except Exception:
        pass
    return save_auth_cache(status, version)


# ------------------------------------------------------------------ settings files
def _settings_paths(cwd: str):
    from pathlib import Path

    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    paths = [("user", base / "settings.json")]
    if cwd:
        paths += [("project", Path(cwd) / ".claude" / "settings.json"),
                  ("local", Path(cwd) / ".claude" / "settings.local.json")]
    return paths


def key_helper_scopes(cwd: str = ""):
    """Settings scopes that define `apiKeyHelper` (presence only; the command is never read out)."""
    found = []
    for scope, path in _settings_paths(cwd):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("apiKeyHelper"):
            found.append(scope)
    return found


# ------------------------------------------------------------------ detection
def detect_claude_code(env, cwd: str = "", config=None, auth=None) -> BillingContext:
    """
    Billing context for a Claude Code session from presence-only signals.
    `auth` is the cached `claude auth status` (read from disk when None).
    """
    evidence = []
    if _flag(env, "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"):
        evidence.append("env:CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 (credentials hidden from hooks)")
    blind = bool(evidence)

    detected = None
    for name, provider, label in CLOUD_FLAGS:
        if _flag(env, name):
            evidence.append(f"env:{name}=1 ({label})")
            if detected is None:
                detected = BillingContext("cloud", provider, "high", (), "detected")
    if detected is None:
        api_signals = []
        for name in KEY_VARS:
            if _present(env, name):
                api_signals.append(name)
                evidence.append(f"env:{name} present")
        helpers = key_helper_scopes(cwd)
        for scope in helpers:
            evidence.append(f"settings:{scope} apiKeyHelper present")
        base_url = str(env.get("ANTHROPIC_BASE_URL", "")).strip()
        if base_url:
            evidence.append(f"env:ANTHROPIC_BASE_URL present ({'this machine' if _is_local_url(base_url) else 'remote'})")
        headless = str(env.get("CLAUDE_CODE_ENTRYPOINT", "")).strip() == "sdk-cli"
        if "ANTHROPIC_API_KEY" in api_signals and not headless and len(api_signals) == 1 and not helpers:
            # Probed on 2.1.328: `claude -p` (CLAUDE_CODE_ENTRYPOINT=sdk-cli) used the key
            # (apiKeySource ANTHROPIC_API_KEY); with CLAUDE_CODE_ENTRYPOINT=claude-vscode the same
            # key was ignored (apiKeySource none) and the plan was billed. Outside -p a key is used
            # only after the user approves it once, which hooks cannot see: say so, show no dollars.
            i = evidence.index("env:ANTHROPIC_API_KEY present")
            evidence[i] += " (used only if approved in an interactive session)"
            detected = BillingContext("unknown", "anthropic", "low", (), "detected")
        elif api_signals or helpers:
            confidence = "high" if headless and "ANTHROPIC_API_KEY" in api_signals else "medium"
            detected = BillingContext("api", "anthropic", confidence, (), "detected")
        elif base_url:
            detected = BillingContext("api", "unknown", "low", (), "detected")

    if detected is None:
        auth = read_auth_cache() if auth is None else auth
        if isinstance(auth, dict) and auth.get("authMethod") == "claude.ai" and auth.get("subscriptionType") \
                and auth.get("apiProvider") in (None, "firstParty"):
            age = time.time() - float(auth.get("checked_at") or 0)
            day = time.strftime("%Y-%m-%d", time.localtime(float(auth.get("checked_at") or 0)))
            evidence.append(f"claude auth status: claude.ai login, plan {auth.get('subscriptionType')} "
                            f"(checked {day})")
            confidence = "low" if blind else ("high" if age < 86400 else "medium" if age < AUTH_CACHE_FRESH_S
                                              else "low")
            detected = BillingContext("subscription", "anthropic", confidence, (), "detected")
        elif isinstance(auth, dict) and auth.get("authMethod"):
            evidence.append(f"claude auth status: authMethod {auth.get('authMethod')}")

    if detected is None:
        detected = BillingContext("unknown", "unknown", "low", (), "detected")
    detected = detected._replace(evidence=tuple(evidence))
    return apply_override(detected, config)


def apply_override(billing: BillingContext, config=None) -> BillingContext:
    """`billing.mode_override` / `billing.provider_override` in config.json always win."""
    section = (config or {}).get("billing") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        return billing
    mode, provider = section.get("mode_override"), section.get("provider_override")
    changed = {}
    if mode in BILLING_MODES:
        changed["mode"] = mode
    if provider in PROVIDERS:
        changed["provider"] = provider
    if not changed:
        return billing
    evidence = tuple(f"config:billing.{k}_override={v}" for k, v in changed.items()) + billing.evidence
    return billing._replace(confidence="high", source="user_override", evidence=evidence, **changed)


def intended_mode(config=None):
    section = (config or {}).get("billing") if isinstance(config, dict) else None
    value = section.get("intended_mode") if isinstance(section, dict) else None
    return value if value in ("subscription", "api", "cloud") else None


def key_warning(env, config=None):
    """
    The variable names that make this session bill per token although the user
    said they use a plan, or [] when there is nothing to warn about.
    """
    if intended_mode(config) != "subscription":
        return []
    return [name for name in KEY_VARS if _present(env, name)]


def warning_text(names) -> str:
    joined = " and ".join(names)
    return (f"BudgetPilot: {joined} is set, so this session is billed per token to the API, "
            "not against your plan. Unset it and restart Claude Code to use your plan.")


def to_row(billing: BillingContext) -> dict:
    return {"billing_mode": billing.mode, "billing_provider": billing.provider,
            "billing_confidence": billing.confidence, "billing_evidence": json.dumps(list(billing.evidence)),
            "billing_source": billing.source}


def from_row(row) -> BillingContext:
    """BillingContext from a cc_sessions row (dict-like); unknown when the session has none."""
    if not row or not row.get("billing_mode"):
        return BillingContext()
    try:
        evidence = tuple(json.loads(row.get("billing_evidence") or "[]"))
    except (ValueError, TypeError):
        evidence = ()
    return BillingContext(row["billing_mode"], row.get("billing_provider") or "unknown",
                          row.get("billing_confidence") or "low", evidence, row.get("billing_source") or "detected")
