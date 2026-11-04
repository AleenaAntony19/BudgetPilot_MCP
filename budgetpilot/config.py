"""
BudgetPilot Configuration System.
Phase A: Configuration resolution, persistence, and environment overrides.
"""

from enum import Enum
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ExecutionMode(str, Enum):
    ENFORCE = "enforce"
    OBSERVE = "observe"
    OFF = "off"


class ModelTierConfig(BaseModel):
    model: str
    provider: str = "ollama"
    input_cost_per_million: float = 1.00
    output_cost_per_million: float = 2.00
    context_window: int = 8192


class TiersConfig(BaseModel):
    cheap: ModelTierConfig = Field(
        default_factory=lambda: ModelTierConfig(
            model="qwen2.5:3b",
            provider="ollama",
            input_cost_per_million=1.00,
            output_cost_per_million=2.00,
            context_window=8192,
        )
    )
    strong: ModelTierConfig = Field(
        default_factory=lambda: ModelTierConfig(
            model="qwen2.5:14b",
            provider="ollama",
            input_cost_per_million=3.00,
            output_cost_per_million=6.00,
            context_window=16384,
        )
    )


class LimitsConfig(BaseModel):
    max_budget_per_session: float = 5.00
    max_tokens_per_call: int = 8192
    max_tool_calls_per_turn: int = 25
    max_recovery_attempts: int = 3
    max_read_files_per_scope: Dict[str, int] = Field(
        default_factory=lambda: {
            "scope_1": 3,
            "scope_2": 8,
            "scope_3": 20,
        }
    )


class TestCommandsConfig(BaseModel):
    __test__ = False
    default: str = "pytest"
    timeout_seconds: int = 60
    commands: List[str] = Field(
        default_factory=lambda: ["pytest", "npm test", "cargo test"]
    )


class BudgetPilotConfig(BaseModel):
    mode: ExecutionMode = ExecutionMode.OBSERVE
    budget_slider: str = "balanced"  # tight, balanced, loose
    tiers: TiersConfig = Field(default_factory=TiersConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    test_commands: TestCommandsConfig = Field(default_factory=TestCommandsConfig)


DEFAULT_BP_HOME = Path.home() / ".budgetpilot"


def get_bp_home() -> Path:
    """
    Resolve the BudgetPilot home directory.
    Checks the BP_HOME environment variable, defaulting to ~/.budgetpilot.
    Ensures that the directory exists.
    """
    env_home = os.environ.get("BP_HOME")
    if env_home:
        bp_home = Path(env_home).expanduser().resolve()
    else:
        bp_home = DEFAULT_BP_HOME.expanduser().resolve()
    bp_home.mkdir(parents=True, exist_ok=True)
    return bp_home


def load_config(config_path: Optional[Path] = None) -> BudgetPilotConfig:
    """
    Load BudgetPilot configuration from config.json.
    If the file does not exist, a default configuration is generated and saved.
    Supports BP_MODE environment variable override.
    """
    if config_path is None:
        config_path = get_bp_home() / "config.json"
    else:
        config_path = Path(config_path).expanduser().resolve()

    if not config_path.exists():
        cfg = BudgetPilotConfig()
        save_config(cfg, config_path)
    else:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = BudgetPilotConfig.model_validate(data)

    # Apply environment variable override for execution mode
    env_mode = os.environ.get("BP_MODE")
    if env_mode:
        env_mode_clean = env_mode.strip().lower()
        valid_modes = [m.value for m in ExecutionMode]
        if env_mode_clean in valid_modes:
            cfg.mode = ExecutionMode(env_mode_clean)
        else:
            raise ValueError(
                f"Invalid BP_MODE '{env_mode}'. Valid modes are: {valid_modes}"
            )

    return cfg


def save_config(config: BudgetPilotConfig, config_path: Optional[Path] = None) -> None:
    """
    Save BudgetPilot configuration to config.json.
    Ensures the parent directory exists.
    """
    if config_path is None:
        config_path = get_bp_home() / "config.json"
    else:
        config_path = Path(config_path).expanduser().resolve()

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(config.model_dump_json(indent=2))
