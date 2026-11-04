"""
BudgetPilot Agent Orchestration Loop.
Phase B: Multi-turn agent execution with Execute Gate interception, tool dispatch,
telemetry recording, and fail-open resilience.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from budgetpilot.agent.model import ModelResult, chat
from budgetpilot.agent.tools import AGENT_TOOL_SCHEMAS, TOOL_SCHEMAS, execute_tool
from budgetpilot.config import load_config
from budgetpilot.core.gate import check as gate_check
from budgetpilot.core.ledger import CostLedger, record_model_call, record_tool_event
from budgetpilot.core.store import get_connection, init_db, log_fallback_error, safe_execute_write
from budgetpilot.core.tracker import TaskState, TaskStatus, TaskTracker
from budgetpilot.core.types import BpEvent, GateVerdict, GateVerdictStatus, normalize_tool_call

logger = logging.getLogger(__name__)


@dataclass
class AgentLoopResult:
    """
    Final summary outcome of an agent loop execution.
    """
    session_id: str
    prompt: str
    answer: str
    turns: int
    tool_calls_count: int
    read_files: List[str]
    touched_files: List[str]
    total_tokens: int
    prompt_tokens: int
    output_tokens: int
    total_cost_usd: float
    status: str  # "completed", "max_turns_exceeded", "error"
    error: Optional[str] = None
    trajectory: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def run_agent_loop(
    prompt: str,
    repo_path: str = ".",
    mode: str = "enforce",
    max_turns: int = 20,
    session_id: Optional[str] = None,
    db_path: Optional[Union[Path, str]] = None,
    tier: str = "cheap",
    cost_cap_usd: Optional[float] = None,
    budget_slider: Optional[str] = None,
) -> AgentLoopResult:
    """
    Executes a multi-turn agent loop for a repository task.

    Orchestration:
    1. Initializes task state and non-destructive git snapshot.
    2. Sends prompt and tool schemas to the model.
    3. For every tool call:
       - Normalizes to canonical BpEvent.
       - Passes through gate.check() (Phase B interception).
       - Executes sandboxed tool.
       - Records tool_events, gate_decisions, and telemetry.
       - Returns observation back to the model.
    4. Concludes when the model provides a final textual answer or max_turns is reached.
    5. Fails open on any unexpected internal error without crashing the caller.
    """
    repo_root = Path(repo_path).expanduser().resolve()
    init_db(db_path)

    active_session = session_id or f"session_{int(time.time())}"
    tracker = TaskTracker(
        session_id=active_session,
        repo_root=repo_root,
        db_path=db_path,
        mode=mode,
    )
    tracker.state.metadata["prompt"] = prompt

    try:
        tracker.start_task(repo_root=repo_root, mode=mode)
        if budget_slider:
            tracker.state.metadata["budget_slider"] = budget_slider
            safe_execute_write(
                conn_or_path=db_path,
                sql="UPDATE sessions SET budget_slider = ? WHERE session_id = ?;",
                params=(budget_slider, active_session),
            )
    except Exception as exc:
        logger.error("Failed to start task tracker; failing open: %s", exc)
        log_fallback_error(exc, "tracker.start_task", fallback_path=None)

    system_prompt = (
        "You are BudgetPilot, an intelligent software engineering agent.\n"
        "You have access to repository tools to inspect and modify code:\n"
        "- list_dir: explore directory structure\n"
        "- search: search code with ripgrep\n"
        "- read_file: inspect file contents with line numbers\n"
        "- edit_file: perform exact string replacements (must be unique)\n"
        "- run_tests: run automated test suites\n"
        "- request_scope: request a scope expansion (+1 level) when broader exploration is required\n\n"
        "Inspect the repository as needed, execute edits carefully, and verify changes.\n"
        "When your work is done or you have answered the user prompt, provide your final response."
    )

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    total_prompt_tokens = 0
    total_output_tokens = 0
    tool_calls_total = 0
    trajectory: List[Dict[str, Any]] = []
    final_answer = ""
    status = "completed"
    error_msg = None

    try:
        for turn in range(1, max_turns + 1):
            tracker.state.metadata["turn"] = turn
            turn_start_time = time.time()

            # 1. Invoke Model
            model_res: ModelResult = chat(
                messages=messages,
                tools=AGENT_TOOL_SCHEMAS,
                tier=tier,
            )

            total_prompt_tokens += model_res.prompt_tokens
            total_output_tokens += model_res.output_tokens

            # Record model telemetry to database (fail-open)
            try:
                record_model_call(
                    model=model_res.model,
                    prompt=prompt if turn == 1 else f"Turn {turn} observation",
                    response=model_res.content or "[Tool calls emitted]",
                    prompt_eval_tokens=model_res.prompt_tokens,
                    output_tokens=model_res.output_tokens,
                    tier=tier,
                    session_id=active_session,
                    total_time=model_res.total_time,
                    load_time=model_res.load_time,
                    prompt_time=model_res.prompt_time,
                    generation_time=model_res.generation_time,
                    db_path=db_path,
                )
            except Exception as model_rec_exc:
                logger.error("Failed to record model call; failing open: %s", model_rec_exc)
                log_fallback_error(model_rec_exc, "record_model_call")

            # Check cost cap if specified
            if cost_cap_usd is not None and cost_cap_usd > 0.0:
                try:
                    ledger = CostLedger(db_path=db_path)
                    cur_cost = ledger.get_session_cost(active_session)
                    if cur_cost >= cost_cap_usd:
                        status = "cost_cap_exceeded"
                        final_answer = f"Cost cap of ${cost_cap_usd:.6f} exceeded (current spend: ${cur_cost:.6f})."
                        break
                except Exception as cap_exc:
                    logger.debug("Cost cap check error: %s", cap_exc)

            # Check if model emitted tool calls
            tool_calls = model_res.tool_calls

            if not tool_calls:
                # No tool calls -> model finished with textual answer
                final_answer = model_res.content or ""
                messages.append({"role": "assistant", "content": final_answer})
                trajectory.append({
                    "turn": turn,
                    "action": "final_answer",
                    "content": final_answer,
                })
                break

            # Model produced tool calls -> process each call
            # Append assistant message with tool calls to message history
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": model_res.content or "",
                "tool_calls": tool_calls,
            }
            messages.append(assistant_msg)

            for tc in tool_calls:
                tool_calls_total += 1
                t_start = time.time()

                # a. Normalize tool call
                try:
                    event: BpEvent = normalize_tool_call(tc, session_id=active_session)
                    tool_name = event.payload.get("tool_name", "unknown")
                    arguments = event.payload.get("arguments", {})
                    call_id = event.payload.get("call_id") or f"call_{tool_calls_total}"
                except Exception as norm_exc:
                    logger.error("Error normalizing tool call; failing open: %s", norm_exc)
                    log_fallback_error(norm_exc, "normalize_tool_call")
                    tool_name = tc.get("name") or (tc.get("function", {}).get("name") if isinstance(tc.get("function"), dict) else "unknown")
                    arguments = tc.get("arguments") or (tc.get("function", {}).get("arguments") if isinstance(tc.get("function"), dict) else {})
                    call_id = f"call_{tool_calls_total}"
                    event = BpEvent(event_type=tool_name, session_id=active_session, payload={"tool_name": tool_name, "arguments": arguments})

                # b. Execute Gate Check (fail-open)
                try:
                    verdict: GateVerdict = gate_check(event, tracker.state)
                except Exception as gate_exc:
                    logger.error("Gate check error; failing open: %s", gate_exc)
                    log_fallback_error(gate_exc, "gate_check", params={"tool": tool_name})
                    verdict = GateVerdict.allow(reason="Gate error fallback: allow")

                # Persist gate decision (fail-open)
                try:
                    safe_execute_write(
                        conn_or_path=db_path,
                        sql="""
                        INSERT INTO gate_decisions (session_id, tool_name, verdict, reason, original_call)
                        VALUES (?, ?, ?, ?, ?);
                        """,
                        params=(
                            active_session,
                            tool_name,
                            verdict.status.value,
                            verdict.reason,
                            json.dumps(tc if isinstance(tc, (dict, list, str, int, float, bool)) else (tc.model_dump() if hasattr(tc, "model_dump") else (tc.dict() if hasattr(tc, "dict") else str(tc)))),
                        ),
                    )
                except Exception as db_exc:
                    logger.error("Failed to persist gate decision; failing open: %s", db_exc)
                    log_fallback_error(db_exc, "gate_decisions_insert")

                # c. Execute tool according to verdict
                if verdict.status == GateVerdictStatus.DENY:
                    tool_output = f"[BLOCKED BY BUDGETPILOT GATE]: {verdict.reason}"
                    tool_ok = False
                    tool_meta = {"blocked": True, "verdict": "deny"}
                else:
                    target_call = verdict.rewritten_call.arguments if (
                        verdict.status == GateVerdictStatus.REWRITE and verdict.rewritten_call
                    ) else arguments

                    try:
                        tool_output, tool_ok, tool_meta = execute_tool(
                            name=tool_name,
                            arguments=target_call,
                            repo_root=repo_root,
                        )
                    except Exception as exec_exc:
                        tool_output = f"Tool execution error: {exec_exc}"
                        tool_ok = False
                        tool_meta = {"error": str(exec_exc)}

                t_duration = time.time() - t_start

                # d. Update Tracker sets (fail-open)
                try:
                    tracker.record_tool_call(tool_name, arguments)
                except Exception as track_exc:
                    logger.error("Failed to update tracker; failing open: %s", track_exc)
                    log_fallback_error(track_exc, "tracker.record_tool_call")

                # e. Record tool event in ledger (fail-open)
                try:
                    record_tool_event(
                        session_id=active_session,
                        tool_name=tool_name,
                        arguments=arguments,
                        call_id=call_id,
                        result=tool_output,
                        duration=t_duration,
                        is_error=not tool_ok,
                        metadata=tool_meta,
                        db_path=db_path,
                    )
                except Exception as rec_exc:
                    logger.error("Failed to record tool event; failing open: %s", rec_exc)
                    log_fallback_error(rec_exc, "record_tool_event")

                # f. Return tool output back to model
                tool_response_msg: Dict[str, Any] = {
                    "role": "tool",
                    "content": tool_output,
                    "name": tool_name,
                }
                if call_id:
                    tool_response_msg["tool_call_id"] = call_id
                messages.append(tool_response_msg)

                trajectory.append({
                    "turn": turn,
                    "tool": tool_name,
                    "arguments": arguments,
                    "ok": tool_ok,
                    "duration": round(t_duration, 4),
                    "output_preview": tool_output[:300],
                })

        else:
            # Reached max turns without breaking
            status = "max_turns_exceeded"
            final_answer = "Max turns reached before agent completed the task."

        # Conclude task in tracker
        tracker.end_task(
            success=(status == "completed"),
            metadata={
                "turns": turn if "turn" in locals() else 0,
                "tool_calls": tool_calls_total,
                "status": status,
            },
        )

    except Exception as loop_exc:
        logger.error("Unhandled exception in run_agent_loop: %s", loop_exc)
        log_fallback_error(loop_exc, "run_agent_loop", params={"session": active_session})
        status = "error"
        error_msg = str(loop_exc)
        final_answer = f"Agent execution encountered an internal error: {loop_exc}"
        try:
            tracker.end_task(success=False, metadata={"error": str(loop_exc)})
        except Exception:
            pass

    # Aggregate session cost
    ledger = CostLedger(db_path=db_path)
    total_cost_usd = ledger.get_session_cost(active_session)

    return AgentLoopResult(
        session_id=active_session,
        prompt=prompt,
        answer=final_answer,
        turns=turn if "turn" in locals() else 0,
        tool_calls_count=tool_calls_total,
        read_files=sorted(list(tracker.state.read_set)),
        touched_files=sorted(list(tracker.state.touched_set)),
        total_tokens=total_prompt_tokens + total_output_tokens,
        prompt_tokens=total_prompt_tokens,
        output_tokens=total_output_tokens,
        total_cost_usd=round(total_cost_usd, 6),
        status=status,
        error=error_msg,
        trajectory=trajectory,
    )
