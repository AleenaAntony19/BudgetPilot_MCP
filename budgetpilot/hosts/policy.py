"""
What a report may show, decided in one place.

Every surface that shows Claude Code usage (dashboard, `bp report`,
`bp transcript-check`, `bp_status`, the A/B report and docs/RESULTS.md) calls
`report_policy` and follows it. None of them decides on its own whether to
show dollars.

    billing mode            tokens, turns, pass rate   billing units                 dollars
    subscription / unknown  yes                        yes, "relative weighting,     never
                                                       not dollars"
    api / cloud             yes                        yes                           "API-equivalent estimate";
                                                                                     "reconciled" only after a
                                                                                     reconciliation check (Phase C)

A session whose billing changed between start and end ("mixed") shows no
dollars and is left out of dollar totals.

Standard library only.
"""

from __future__ import annotations

UNITS_RELATIVE = "billing units (relative weighting, not dollars)"
UNITS_PLAIN = "billing units"
DOLLARS_ESTIMATE = "API-equivalent estimate"
DOLLARS_RECONCILED = "API-equivalent, reconciled"


def report_policy(billing=None, capabilities=None, mixed: bool = False, reconciled: bool = False) -> dict:
    """
    billing       a BillingContext (or None = unknown)
    capabilities  a HostCapabilities (or None = Claude Code, tier enforce)
    mixed         the session's billing changed mid-session
    reconciled    the computed dollars passed a reconciliation check
    """
    mode = getattr(billing, "mode", None) or "unknown"
    per_token = mode in ("api", "cloud")
    show_dollars = per_token and not mixed
    tier = getattr(capabilities, "tier", None) or "enforce"
    if mixed:
        note = "Billing changed during this session; no dollar figures are shown."
    elif per_token:
        note = "Dollar figures are estimates from list prices, not your invoice."
    else:
        note = "This session is not billed per token, so no dollar figures are shown."
    return {
        "billing_mode": mode,
        "billing_provider": getattr(billing, "provider", None) or "unknown",
        "billing_confidence": getattr(billing, "confidence", None) or "low",
        "set_by_user": getattr(billing, "source", "") == "user_override",
        "mixed": bool(mixed),
        "show_tokens": True,
        "show_billing_units": True,
        "units_label": UNITS_PLAIN if per_token else UNITS_RELATIVE,
        "show_dollars": show_dollars,
        "dollars_label": (DOLLARS_RECONCILED if reconciled else DOLLARS_ESTIMATE) if show_dollars else None,
        "reconciled": bool(reconciled and show_dollars),
        "show_gate": tier == "enforce",
        "show_recovery": tier == "enforce",
        "tier": tier,
        "note": note,
    }


def money(value, policy: dict, digits: int = 4):
    """A dollar string when the policy allows dollars, else None. Callers print nothing for None."""
    if not policy.get("show_dollars") or value is None:
        return None
    return f"${float(value):.{digits}f}"


DOLLAR_KEYS = ("ref_usd", "cost_usd", "total_cost_usd", "total_cost_usd_reported", "usd")


def strip_dollars(obj, policy: dict):
    """Copy of a JSON-like structure with every dollar field removed when the policy forbids dollars."""
    if policy.get("show_dollars"):
        return obj
    if isinstance(obj, dict):
        return {k: strip_dollars(v, policy) for k, v in obj.items() if k not in DOLLAR_KEYS}
    if isinstance(obj, list):
        return [strip_dollars(v, policy) for v in obj]
    return obj
