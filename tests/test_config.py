"""
Unit tests for BudgetPilot configuration system (Phase A).
"""

import json
import os
from pathlib import Path
import tempfile
import pytest

from budgetpilot.config import (
    BudgetPilotConfig,
    ExecutionMode,
    LimitsConfig,
    ModelTierConfig,
    TestCommandsConfig,
    TiersConfig,
    get_bp_home,
    load_config,
    save_config,
)


def test_default_bp_home(monkeypatch):
    """Test resolution of BP_HOME with default and environment variable override."""
    # Test default
    monkeypatch.delenv("BP_HOME", raising=False)
    default_home = Path.home() / ".budgetpilot"
    assert get_bp_home() == default_home.resolve()
    assert default_home.exists()

    # Test override
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_home = Path(tmpdir) / "custom_bp"
        monkeypatch.setenv("BP_HOME", str(custom_home))
        resolved = get_bp_home()
        assert resolved == custom_home.resolve()
        assert custom_home.exists()


def test_config_defaults():
    """Verify standard default settings per PRD specifications."""
    cfg = BudgetPilotConfig()

    # Mode defaults to observe
    assert cfg.mode == ExecutionMode.OBSERVE
    assert cfg.budget_slider == "balanced"

    # Tiers
    assert cfg.tiers.cheap.model == "qwen2.5:3b"
    assert cfg.tiers.cheap.provider == "ollama"
    assert cfg.tiers.cheap.input_cost_per_million == 1.00
    assert cfg.tiers.cheap.output_cost_per_million == 2.00

    assert cfg.tiers.strong.model == "qwen2.5:14b"
    assert cfg.tiers.strong.provider == "ollama"
    assert cfg.tiers.strong.input_cost_per_million == 3.00
    assert cfg.tiers.strong.output_cost_per_million == 6.00

    # Limits
    assert cfg.limits.max_budget_per_session == 5.00
    assert cfg.limits.max_tokens_per_call == 8192
    assert cfg.limits.max_tool_calls_per_turn == 25
    assert cfg.limits.max_recovery_attempts == 3
    assert cfg.limits.max_read_files_per_scope["scope_1"] == 3
    assert cfg.limits.max_read_files_per_scope["scope_2"] == 8
    assert cfg.limits.max_read_files_per_scope["scope_3"] == 20

    # Test commands
    assert cfg.test_commands.default == "pytest"
    assert cfg.test_commands.timeout_seconds == 60
    assert "pytest" in cfg.test_commands.commands


def test_save_and_load_config():
    """Verify clean save and load roundtrip to JSON."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "config.json"

        # Create custom config
        cfg = BudgetPilotConfig(
            mode=ExecutionMode.ENFORCE,
            budget_slider="tight",
        )
        cfg.limits.max_recovery_attempts = 5
        cfg.test_commands.default = "pytest -v"

        save_config(cfg, cfg_file)
        assert cfg_file.exists()

        # Load back
        loaded = load_config(cfg_file)
        assert loaded.mode == ExecutionMode.ENFORCE
        assert loaded.budget_slider == "tight"
        assert loaded.limits.max_recovery_attempts == 5
        assert loaded.test_commands.default == "pytest -v"


def test_load_creates_default_if_missing():
    """Verify that load_config creates config.json with defaults if missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "sub" / "config.json"
        assert not cfg_file.exists()

        loaded = load_config(cfg_file)
        assert cfg_file.exists()
        assert loaded.mode == ExecutionMode.OBSERVE


def test_bp_mode_env_override(monkeypatch):
    """Verify BP_MODE environment variable overrides saved configuration."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "config.json"
        # Save as observe
        cfg = BudgetPilotConfig(mode=ExecutionMode.OBSERVE)
        save_config(cfg, cfg_file)

        # Override with enforce
        monkeypatch.setenv("BP_MODE", "enforce")
        loaded_enforce = load_config(cfg_file)
        assert loaded_enforce.mode == ExecutionMode.ENFORCE

        # Override with off
        monkeypatch.setenv("BP_MODE", "off")
        loaded_off = load_config(cfg_file)
        assert loaded_off.mode == ExecutionMode.OFF

        # Invalid mode should raise ValueError
        monkeypatch.setenv("BP_MODE", "unsupported_mode")
        with pytest.raises(ValueError, match="Invalid BP_MODE"):
            load_config(cfg_file)
