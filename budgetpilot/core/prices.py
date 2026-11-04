"""
BudgetPilot Reference Price Subsystem.
Phase A: Reference USD pricing for model tiers and price configuration management.

Strict Rule: This module must NOT import ollama, mcp, or fastapi.
"""

from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

from budgetpilot.config import get_bp_home

logger = logging.getLogger(__name__)


@dataclass
class TierPrice:
    """
    Reference pricing per million tokens in USD.
    """
    input_per_million: float
    output_per_million: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TierPrice":
        return cls(
            input_per_million=float(data.get("input_per_million", 1.00)),
            output_per_million=float(data.get("output_per_million", 2.00)),
        )


DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    "cheap": {
        "input_per_million": 1.00,
        "output_per_million": 2.00,
    },
    "strong": {
        "input_per_million": 3.00,
        "output_per_million": 6.00,
    },
}


def resolve_prices_path(path: Optional[Union[Path, str]] = None) -> Path:
    """
    Resolve path to prices.json (defaults to ~/.budgetpilot/prices.json).
    """
    if path is None:
        return get_bp_home() / "prices.json"
    return Path(path).expanduser().resolve()


def load_prices(path: Optional[Union[Path, str]] = None) -> Dict[str, TierPrice]:
    """
    Load tier pricing dictionary from prices.json.
    Creates default file if it does not exist.
    """
    prices_path = resolve_prices_path(path)

    if not prices_path.exists():
        save_prices(DEFAULT_PRICES, prices_path)
        return {k: TierPrice.from_dict(v) for k, v in DEFAULT_PRICES.items()}

    try:
        with open(prices_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        prices: Dict[str, TierPrice] = {}
        for tier_name, tier_vals in data.items():
            prices[tier_name.lower()] = TierPrice.from_dict(tier_vals)
        # Ensure standard tiers exist
        for standard_tier, standard_vals in DEFAULT_PRICES.items():
            if standard_tier not in prices:
                prices[standard_tier] = TierPrice.from_dict(standard_vals)
        return prices
    except Exception as exc:
        logger.warning("Failed to parse %s, falling back to defaults: %s", prices_path, exc)
        return {k: TierPrice.from_dict(v) for k, v in DEFAULT_PRICES.items()}


def save_prices(
    prices: Dict[str, Union[TierPrice, Dict[str, float]]],
    path: Optional[Union[Path, str]] = None,
) -> None:
    """
    Save tier pricing dictionary to prices.json.
    """
    prices_path = resolve_prices_path(path)
    prices_path.parent.mkdir(parents=True, exist_ok=True)

    serializable: Dict[str, Dict[str, float]] = {}
    for tier, val in prices.items():
        if isinstance(val, TierPrice):
            serializable[tier.lower()] = val.to_dict()
        elif isinstance(val, dict):
            serializable[tier.lower()] = {
                "input_per_million": float(val.get("input_per_million", 1.00)),
                "output_per_million": float(val.get("output_per_million", 2.00)),
            }

    with open(prices_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2)


def price_call(
    tier: str,
    in_tokens: int,
    out_tokens: int,
    prices_path: Optional[Union[Path, str]] = None,
) -> float:
    """
    Calculate reference USD cost for a model call given tier, input tokens, and output tokens.
    """
    tier_key = (tier or "cheap").lower()
    prices = load_prices(prices_path)

    tier_price = prices.get(tier_key, prices.get("cheap", TierPrice(1.00, 2.00)))

    input_cost = (max(0, in_tokens) / 1_000_000.0) * tier_price.input_per_million
    output_cost = (max(0, out_tokens) / 1_000_000.0) * tier_price.output_per_million
    return round(input_cost + output_cost, 8)
