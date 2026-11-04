"""
Unit tests for BudgetPilot Ollama model wrapper and safe null-handling (Phase A).
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import pytest

from budgetpilot.agent.model import ModelResult, chat


class MockMessage:
    def __init__(self, content: str = "", tool_calls: Optional[List[Dict[str, Any]]] = None):
        self.content = content
        self.tool_calls = tool_calls


class MockOllamaResponse:
    def __init__(
        self,
        content: str = "Test response",
        prompt_eval_count: Optional[int] = 42,
        eval_count: Optional[int] = 18,
        total_duration: Optional[int] = 1_500_000_000,
        load_duration: Optional[int] = 400_000_000,
        prompt_eval_duration: Optional[int] = 100_000_000,
        eval_duration: Optional[int] = 1_000_000_000,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ):
        self.message = MockMessage(content=content, tool_calls=tool_calls)
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count
        self.total_duration = total_duration
        self.load_duration = load_duration
        self.prompt_eval_duration = prompt_eval_duration
        self.eval_duration = eval_duration


def test_chat_normal_execution(monkeypatch):
    """Verify chat correctly invokes ollama.chat and constructs ModelResult."""
    recorded_call = {}

    def mock_ollama_chat(**kwargs):
        recorded_call.update(kwargs)
        return MockOllamaResponse(
            content="Hello from Qwen",
            prompt_eval_count=50,
            eval_count=20,
            total_duration=2_000_000_000,
            load_duration=500_000_000,
            prompt_eval_duration=100_000_000,
            eval_duration=1_400_000_000,
        )

    monkeypatch.setattr("ollama.chat", mock_ollama_chat)

    messages = [{"role": "user", "content": "Hello"}]
    res = chat(messages=messages, tier="cheap")

    assert isinstance(res, ModelResult)
    assert res.content == "Hello from Qwen"
    assert res.tier == "cheap"
    assert res.model == "qwen2.5:3b"
    assert res.prompt_tokens == 50
    assert res.output_tokens == 20
    assert res.total_tokens == 70

    assert res.total_time == 2.0
    assert res.load_time == 0.5
    assert res.prompt_time == 0.1
    assert res.generation_time == 1.4

    # Verify kwargs sent to ollama.chat
    assert recorded_call["model"] == "qwen2.5:3b"
    assert recorded_call["messages"] == messages


def test_chat_tier_strong_resolution(monkeypatch):
    """Verify strong tier resolves to configured strong model."""
    recorded_call = {}

    def mock_ollama_chat(**kwargs):
        recorded_call.update(kwargs)
        return MockOllamaResponse(content="Strong response")

    monkeypatch.setattr("ollama.chat", mock_ollama_chat)

    res = chat(messages=[{"role": "user", "content": "Complex logic"}], tier="strong")
    assert res.tier == "strong"
    assert res.model == "qwen2.5:14b"
    assert recorded_call["model"] == "qwen2.5:14b"


def test_chat_safe_null_duration_and_token_handling(monkeypatch):
    """
    CRITICAL REQUIREMENT: Make all duration and token returns safely handle
    None values by converting None to 0.
    """
    def mock_ollama_chat_with_nones(**kwargs):
        # Simulate Ollama response where all metrics are None
        return MockOllamaResponse(
            content="Response with missing metrics",
            prompt_eval_count=None,
            eval_count=None,
            total_duration=None,
            load_duration=None,
            prompt_eval_duration=None,
            eval_duration=None,
        )

    monkeypatch.setattr("ollama.chat", mock_ollama_chat_with_nones)

    res = chat(messages=[{"role": "user", "content": "Hi"}], tier="cheap")

    assert res.prompt_tokens == 0
    assert res.output_tokens == 0
    assert res.total_tokens == 0
    assert res.total_duration_ns == 0
    assert res.load_duration_ns == 0
    assert res.prompt_eval_duration_ns == 0
    assert res.eval_duration_ns == 0
    assert res.total_time == 0.0
    assert res.load_time == 0.0
    assert res.prompt_time == 0.0
    assert res.generation_time == 0.0


def test_chat_dict_style_response_and_tools(monkeypatch):
    """Verify handling when Ollama returns dict response and tool calls."""
    def mock_ollama_chat_dict(**kwargs):
        return {
            "message": {
                "content": "Tool calling response",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "main.py"}}],
            },
            "prompt_eval_count": 30,
            "eval_count": 15,
            "total_duration": 1_000_000_000,
            "load_duration": 200_000_000,
            "prompt_eval_duration": 100_000_000,
            "eval_duration": 700_000_000,
        }

    monkeypatch.setattr("ollama.chat", mock_ollama_chat_dict)

    tools = [{"type": "function", "function": {"name": "read_file"}}]
    options = {"temperature": 0.0}

    res = chat(
        messages=[{"role": "user", "content": "Inspect file"}],
        tools=tools,
        options=options,
        tier="cheap",
    )

    assert res.content == "Tool calling response"
    assert res.tool_calls is not None
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0]["name"] == "read_file"
    assert res.prompt_tokens == 30
    assert res.output_tokens == 15
    assert res.total_time == 1.0
