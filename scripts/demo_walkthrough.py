#!/usr/bin/env python3
"""
BudgetPilot: End-to-End Pipeline Demo Walkthrough.

A standalone, runnable demonstration showing how each core module of BudgetPilot works:
1. Scope Estimation (Lexical + Ripgrep heuristic probe)
2. Execute Gate (Policy Enforcement Matrix, 400-line clamping, 3-turn overrides)
3. Test Verification & Diagnostics (7-class failure taxonomy & signature hashing)
4. Cost-Calibrated Recovery Routing (Utility maximization U(a) = P - λ*C)
5. Avoided Cost Financial Modeling (p25-p75 uncertainty bands)
6. Dynamic Conformal Budget Dial (tight, balanced, loose)

Usage:
    python scripts/demo_walkthrough.py
"""

from pathlib import Path
import sys
import tempfile
import time

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from budgetpilot.core.estimator import ScopeEstimator, estimate
from budgetpilot.core.executors import execute_expand_scope, execute_reflect, execute_replan
from budgetpilot.core.gate import check as gate_check
from budgetpilot.core.router import (
    ACTION_COSTS,
    BASE_PROBABILITIES,
    HandSetLambda,
    route,
)
from budgetpilot.core.safety import redact_secrets
from budgetpilot.core.savings import AvoidedEstimate, compute_avoided_estimate
from budgetpilot.core.store import init_db
from budgetpilot.core.tracker import TaskState
from budgetpilot.core.types import (
    BpEvent,
    FailureClass,
    GateVerdictStatus,
    RecoveryAction,
    RecoveryContext,
    ToolCall,
)
from budgetpilot.core.verifier import parse_pytest_output


def header(title: str, subtitle: str = "") -> None:
    print("\n" + "=" * 76)
    print(f"  {title.upper()}")
    if subtitle:
        print(f"  {subtitle}")
    print("=" * 76)


def section(step_num: int, title: str) -> None:
    print(f"\n--- [STEP {step_num}] {title} " + "-" * (65 - len(title)))


def main() -> int:
    header(
        "BudgetPilot Architectural Walkthrough",
        "Demonstrating Scope Estimation, Execute Gate, Verifier, Router, and Savings",
    )

    # -------------------------------------------------------------------------
    # STEP 1: Scope Estimation
    # -------------------------------------------------------------------------
    section(1, "Pre-Execution Scope Estimation")
    print("Scenario: A developer provides two distinct task prompts to the agent.\n")

    prompt_simple = "Fix IndexError in calculate_discount when the cart contains 0 items in cart.py"
    prompt_complex = (
        "Refactor global authentication, migrate session tokens across database, "
        "and update middleware security handlers across all endpoints"
    )

    pred_simple = estimate(prompt=prompt_simple, repo_path=str(REPO_ROOT))
    pred_complex = estimate(prompt=prompt_complex, repo_path=str(REPO_ROOT))

    print(f"Task A (Simple):   \"{prompt_simple[:60]}...\"")
    print(f"  -> Predicted Scope: Level {pred_simple.scope} ({'Small' if pred_simple.scope == 1 else 'Medium'})")
    print(f"  -> Token Budget:    {pred_simple.budget_tokens:,} tokens")
    print(f"  -> Max Read Files:  {pred_simple.max_read_files} files")
    print(f"  -> Rationale:       {pred_simple.rationale}\n")

    print(f"Task B (Complex):  \"{prompt_complex[:60]}...\"")
    print(f"  -> Predicted Scope: Level {pred_complex.scope} (Large / Cross-Cutting)")
    print(f"  -> Token Budget:    {pred_complex.budget_tokens:,} tokens")
    print(f"  -> Max Read Files:  {pred_complex.max_read_files} files")
    print(f"  -> Rationale:       {pred_complex.rationale}")

    # -------------------------------------------------------------------------
    # STEP 2: Execute Gate Policy Enforcement
    # -------------------------------------------------------------------------
    section(2, "Execute Gate Policy Enforcement & Window Rewriting")
    print("Scenario: Agent attempts various tool calls during a Scope 1 task.\n")

    state = TaskState(session_id="demo_walkthrough_sess")
    state.metadata["scope"] = 1
    state.metadata["named_files"] = ["cart.py"]

    # Case A: Windowed read within limits
    call_a = BpEvent(
        event_type="read_file",
        payload={"tool_name": "read_file", "arguments": {"path": "cart.py", "start": 1, "end": 120}},
    )
    verdict_a = gate_check(call_a, state)
    print(f"[Case A: Windowed read of named file]")
    print(f"  Call:    read_file(path='cart.py', start=1, end=120)")
    print(f"  Verdict: {verdict_a.status.value.upper()} -> {verdict_a.reason}")

    # Case B: Over-broad unwindowed read (clamped to 400 lines)
    call_b = BpEvent(
        event_type="read_file",
        payload={"tool_name": "read_file", "arguments": {"path": "utils.py", "start": 1, "end": 2000}},
    )
    verdict_b = gate_check(call_b, state)
    print(f"\n[Case B: Over-broad read exceeding 400 lines]")
    print(f"  Call:    read_file(path='utils.py', start=1, end=2000)")
    print(f"  Verdict: {verdict_b.status.value.upper()} -> {verdict_b.reason}")
    if verdict_b.rewritten_call:
        print(f"  Rewrite: Clamped to lines {verdict_b.rewritten_call.arguments.get('start')}..{verdict_b.rewritten_call.arguments.get('end')}")

    # Case C: Exceeding max 3 read files in Scope 1
    state.read_set.update(["file1.py", "file2.py", "file3.py"])
    call_c = BpEvent(
        event_type="read_file",
        payload={"tool_name": "read_file", "arguments": {"path": "unrelated_fourth.py", "start": 1, "end": 100}},
    )
    verdict_c = gate_check(call_c, state)
    print(f"\n[Case C: Exceeding Scope 1 read limit (max 3 files outside named set)]")
    print(f"  Call:    read_file(path='unrelated_fourth.py')")
    print(f"  Verdict: {verdict_c.status.value.upper()} -> {verdict_c.reason}")

    # Case D: Repeating a denied call within 3 turns triggers the safety override bypass
    verdict_d = gate_check(call_c, state)
    print(f"\n[Case D: Repeating the denied call within 3 turns (Override Bypass)]")
    print(f"  Call:    read_file(path='unrelated_fourth.py') [repeated attempt]")
    print(f"  Verdict: {verdict_d.status.value.upper()} -> {verdict_d.reason}")

    # -------------------------------------------------------------------------
    # STEP 3: Test Verification & Failure Diagnostics
    # -------------------------------------------------------------------------
    section(3, "Test Verifier & Failure Diagnostics")
    print("Scenario: Agent modifies code and runs pytest. Test fails.\n")

    simulated_pytest_output = """
============================= test session starts ==============================
collected 2 items

tests/test_cart.py .F                                                    [100%]

=================================== FAILURES ===================================
__________________________ test_zero_quantity_discount __________________________

    def test_zero_quantity_discount():
        cart = ShoppingCart()
>       discount = calculate_discount(cart)
E       NameError: name 'calculate_discount' is not defined

tests/test_cart.py:18: NameError
=========================== short test summary info ============================
FAILED tests/test_cart.py::test_zero_quantity_discount - NameError: name 'calculate_discount' is not defined
========================= 1 failed, 1 passed in 0.04s ==========================
"""

    from budgetpilot.core.verifier import classify_failure, compute_signature_hash, parse_pytest_output

    parsed = parse_pytest_output(simulated_pytest_output, touched_set={"cart.py"})
    sig_hash = compute_signature_hash(parsed["error_type"], parsed["top_in_repo_frame"])
    failure_class, is_actionable = classify_failure(
        error_type=parsed["error_type"],
        failure_message=parsed["failure_message"],
        failing_test_ids=parsed["failing_test_ids"],
        top_in_repo_frame=parsed["top_in_repo_frame"],
        outside_frames=parsed["outside_frames"],
        unread_symbols=parsed["unread_symbols"],
        touched_set={"cart.py"},
        signature_hash=sig_hash,
    )

    print(f"  Exit Code:       1 (FAILED)")
    print(f"  Failure Class:   {failure_class.value.upper()}")
    print(f"  Signature Hash:  {sig_hash[:16]}... (Deterministic SHA-256)")
    print(f"  Failed Tests:    {', '.join(parsed['failing_test_ids'])}")
    print(f"  Primary Error:   {parsed['failure_message']}")

    # -------------------------------------------------------------------------
    # STEP 4: Cost-Calibrated Recovery Routing
    # -------------------------------------------------------------------------
    section(4, "Cost-Calibrated Recovery Routing")
    print("Scenario: The router evaluates candidate recovery actions using utility model:")
    print("  U(a) = P_success(a) - λ * C_norm(a)")
    print(f"  Action Costs: Reflect={ACTION_COSTS[RecoveryAction.REFLECT]}, "
          f"Replan={ACTION_COSTS[RecoveryAction.REPLAN]}, "
          f"Expand-Scope={ACTION_COSTS[RecoveryAction.EXPAND_SCOPE]}, "
          f"Escalate={ACTION_COSTS[RecoveryAction.ESCALATE]}\n")

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "demo.db"
        init_db(db_path)

        # 4A. Local syntax/assertion error -> Routes to REFLECT
        ctx_syntax = RecoveryContext(
            session_id="demo_sess",
            failure_class=FailureClass.SYNTAX_OR_LINT,
            attempt_number=1,
            files_touched=["cart.py"],
            stderr="SyntaxError: invalid syntax",
        )
        dec_syntax = route(ctx_syntax, budget="balanced", db_path=db_path)
        print(f"[Scenario 1: Local Syntax Error]")
        print(f"  Failure Class: {ctx_syntax.failure_class.value}")
        print(f"  Selected:      {dec_syntax.action.value.upper()} (Target Tier: {dec_syntax.target_tier})")
        print(f"  Rationale:     {dec_syntax.rationale}")

        # Directive generated
        directive_reflect = execute_reflect(ctx_syntax, test_output="SyntaxError: invalid syntax in cart.py:24")
        print(f"  Directive Injected:\n    " + "\n    ".join(directive_reflect.splitlines()[-4:]))

        # 4B. Symbol outside touched files -> Routes to EXPAND_SCOPE
        ctx_scope = RecoveryContext(
            session_id="demo_sess",
            failure_class=FailureClass.SYMBOL_OUTSIDE_TOUCHED,
            attempt_number=1,
            files_touched=["cart.py"],
            stderr="ImportError: cannot import name 'pricing_rules' from 'billing.pricing'",
        )
        dec_scope = route(ctx_scope, budget="balanced", db_path=db_path)
        print(f"\n[Scenario 2: Missing External Symbol]")
        print(f"  Failure Class: {ctx_scope.failure_class.value}")
        print(f"  Selected:      {dec_scope.action.value.upper()} (Target Tier: {dec_scope.target_tier})")
        print(f"  Rationale:     {dec_scope.rationale}")

        frontier = execute_expand_scope(ctx_scope, repo_root=str(REPO_ROOT), current_scope=1)
        print(f"  Frontier Action: Bumped Scope {frontier.previous_scope} -> {frontier.new_scope} "
              f"(New Budget: {frontier.new_budget_tokens:,} tokens, Max Reads: {frontier.new_max_read_files})")

        # 4C. Repeated persistent failures / capability gap -> Routes to ESCALATE
        ctx_gap = RecoveryContext(
            session_id="demo_sess",
            failure_class=FailureClass.CAPABILITY_GAP,
            attempt_number=3,
            files_touched=["cart.py"],
            previous_actions=[RecoveryAction.REFLECT, RecoveryAction.REPLAN],
        )
        dec_gap = route(ctx_gap, budget="loose", db_path=db_path)
        print(f"\n[Scenario 3: Capability Gap After Multiple Attempts]")
        print(f"  Failure Class: {ctx_gap.failure_class.value}")
        print(f"  Selected:      {dec_gap.action.value.upper()} (Target Tier: {dec_gap.target_tier})")
        print(f"  Rationale:     {dec_gap.rationale}")

    # -------------------------------------------------------------------------
    # STEP 5: Dynamic Conformal Budget Dial
    # -------------------------------------------------------------------------
    section(5, "Dynamic Conformal Budget Dial (Zero Retraining)")
    print("Scenario: Moving the runtime budget slider adjusts the cost penalty λ:")
    print("  tight (λ = 2.50)    -> Aggressively prioritizes low token spend")
    print("  balanced (λ = 0.50) -> Default balanced cost/solve-rate tradeoff")
    print("  loose (λ = 0.10)    -> Prioritizes solve rate, lower penalty for escalation\n")

    test_ctx = RecoveryContext(
        session_id="demo_sess",
        failure_class=FailureClass.REPEAT_SIGNATURE,
        attempt_number=2,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "demo.db"
        init_db(db_path)
        for dial in ["tight", "balanced", "loose"]:
            decision = route(test_ctx, budget=dial, db_path=db_path)
            lam = HandSetLambda.get(dial)
            print(f"  Slider = {dial:8s} (λ={lam:.2f}) -> Picked Action: {decision.action.value:12s} "
                  f"(Target Tier: {decision.target_tier})")

    # -------------------------------------------------------------------------
    # STEP 6: Avoided Cost Financial Modeling & Uncertainty Bands
    # -------------------------------------------------------------------------
    section(6, "Avoided Cost Financial Modeling & Uncertainty Bands")
    print("Scenario: A session with 4 gate denies and 2 rewrites models counterfactual savings:")
    print("  Uncertainty Band Invariant: low_usd <= avoided_usd <= high_usd\n")
    sample_avoided = AvoidedEstimate(
        session_id="demo_sess",
        mechanism="combined",
        measured_spend_usd=0.004200,
        avoided_mid_usd=0.012500,
        avoided_low_usd=0.008400,
        avoided_high_usd=0.016800,
        deny_count=4,
        rewrite_count=2,
        override_count=1,
        net_tokens_avoided=12500,
    )
    savings_ratio = sample_avoided.avoided_mid_usd / (sample_avoided.measured_spend_usd + sample_avoided.avoided_mid_usd)
    print(f"  Measured Spend:       $ {sample_avoided.measured_spend_usd:.6f} USD (Exact API usage)")
    print(f"  Modeled Avoided:      $ {sample_avoided.avoided_mid_usd:.6f} USD (Point estimate)")
    print(f"  Uncertainty Band:     [$ {sample_avoided.avoided_low_usd:.6f}, $ {sample_avoided.avoided_high_usd:.6f}] USD (p25-p75)")
    print(f"  Gate Denies/Rewrites: {sample_avoided.deny_count} denies, {sample_avoided.rewrite_count} rewrites, {sample_avoided.override_count} overrides deducted")
    print(f"  Net Tokens Avoided:   {sample_avoided.net_tokens_avoided:,} tokens")
    print(f"  Modeled Savings:      {savings_ratio * 100:.1f}% (avoided / [measured + avoided])")

    # -------------------------------------------------------------------------
    # STEP 7: Automated Secret Redaction Guarantee
    # -------------------------------------------------------------------------
    section(7, "Automated Secret Redaction Prior to Storage/Logging")
    sample_raw_log = (
        "Agent error: Authentication failed with AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE "
        "and Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDc against service."
    )
    redacted = redact_secrets(sample_raw_log)
    print("Raw Intercepted Text:")
    print(f"  \"{sample_raw_log}\"\n")
    print("Sanitized Before SQLite / fallback.jsonl Logging:")
    print(f"  \"{redacted}\"")

    header(
        "Walkthrough Completed Successfully",
        "BudgetPilot is ready to supervise coding agents with verified cost control!",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
