"""
BudgetPilot LLM Wrapper Subsystem.
Phase A: Safe Ollama chat wrapping, duration normalization, and ModelResult extraction.
"""

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional, Union

import ollama
from budgetpilot.config import load_config

logger = logging.getLogger(__name__)


@dataclass
class ModelResult:
    """
    Standardized LLM execution result.
    Guarantees all duration and token counts are non-null numeric values (converting None to 0).
    """
    content: str
    model: str
    tier: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    total_time: float = 0.0
    load_time: float = 0.0
    prompt_time: float = 0.0
    generation_time: float = 0.0
    total_duration_ns: int = 0
    load_duration_ns: int = 0
    prompt_eval_duration_ns: int = 0
    eval_duration_ns: int = 0
    tool_calls: Optional[List[Dict[str, Any]]] = None
    raw_response: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "model": self.model,
            "tier": self.tier,
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "total_time": self.total_time,
            "load_time": self.load_time,
            "prompt_time": self.prompt_time,
            "generation_time": self.generation_time,
            "total_duration_ns": self.total_duration_ns,
            "load_duration_ns": self.load_duration_ns,
            "prompt_eval_duration_ns": self.prompt_eval_duration_ns,
            "eval_duration_ns": self.eval_duration_ns,
            "tool_calls": self.tool_calls,
        }


def _extract_val(obj: Any, key: str) -> Any:
    """Safely extract field from either a dict or object."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def chat(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Any]] = None,
    tier: str = "cheap",
    options: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
) -> ModelResult:
    """
    Execute chat completion against Ollama with model resolution, safe null-handling,
    and telemetry normalization.

    - Resolves tier model from config ('cheap' vs 'strong').
    - Converts all None token and duration metrics safely to 0.
    - Returns standardized ModelResult dataclass.
    """
    tier_clean = (tier or "cheap").lower()

    if not model:
        try:
            cfg = load_config()
            if tier_clean == "strong":
                model = cfg.tiers.strong.model
            else:
                model = cfg.tiers.cheap.model
        except Exception as exc:
            logger.debug("Could not resolve tier model from config: %s", exc)
            model = "qwen2.5:14b" if tier_clean == "strong" else "qwen2.5:3b"

    call_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if tools is not None:
        call_kwargs["tools"] = tools
    if options is not None:
        call_kwargs["options"] = options

    response = ollama.chat(**call_kwargs)

    # Content extraction
    message = _extract_val(response, "message")
    content = ""
    tool_calls = None
    if message is not None:
        if isinstance(message, dict):
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls")
        else:
            content = getattr(message, "content", "") or ""
            tool_calls = getattr(message, "tool_calls", None)

        # Convert tool call objects (e.g. Pydantic models from ollama) to plain dicts
        if tool_calls is not None:
            normalized_tool_calls = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    normalized_tool_calls.append(tc)
                elif hasattr(tc, "model_dump"):
                    normalized_tool_calls.append(tc.model_dump())
                elif hasattr(tc, "dict"):
                    normalized_tool_calls.append(tc.dict())
                else:
                    normalized_tool_calls.append(tc)
            tool_calls = normalized_tool_calls

    # Safely convert None token counts to 0
    prompt_tokens = int(_extract_val(response, "prompt_eval_count") or 0)
    output_tokens = int(_extract_val(response, "eval_count") or 0)
    total_tokens = prompt_tokens + output_tokens

    # Safely convert None duration counts to 0 nanoseconds
    total_duration_ns = int(_extract_val(response, "total_duration") or 0)
    load_duration_ns = int(_extract_val(response, "load_duration") or 0)
    prompt_eval_duration_ns = int(_extract_val(response, "prompt_eval_duration") or 0)
    eval_duration_ns = int(_extract_val(response, "eval_duration") or 0)

    # Normalization to seconds
    total_time = total_duration_ns / 1_000_000_000.0
    load_time = load_duration_ns / 1_000_000_000.0
    prompt_time = prompt_eval_duration_ns / 1_000_000_000.0
    generation_time = eval_duration_ns / 1_000_000_000.0

    return ModelResult(
        content=content,
        model=model,
        tier=tier_clean,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        total_time=round(total_time, 6),
        load_time=round(load_time, 6),
        prompt_time=round(prompt_time, 6),
        generation_time=round(generation_time, 6),
        total_duration_ns=total_duration_ns,
        load_duration_ns=load_duration_ns,
        prompt_eval_duration_ns=prompt_eval_duration_ns,
        eval_duration_ns=eval_duration_ns,
        tool_calls=tool_calls,
        raw_response=response,
    )
