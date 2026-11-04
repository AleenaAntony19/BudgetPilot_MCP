"""
Unit tests for BudgetPilot pricing, ledger telemetry recording, and advisory anomaly detection.
"""

from pathlib import Path
import tempfile
import pytest

from budgetpilot.core.ledger import (
    CostLedger,
    detect_anomalies,
    estimate_prompt_tokens,
    record_model_call,
)
from budgetpilot.core.prices import (
    DEFAULT_PRICES,
    TierPrice,
    load_prices,
    price_call,
    save_prices,
)
from budgetpilot.core.store import get_connection, init_db


def test_prices_load_save():
    """Verify loading and saving tier prices to prices.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prices_file = Path(tmpdir) / "prices.json"

        # Load when file does not exist -> creates default
        prices = load_prices(prices_file)
        assert prices_file.exists()
        assert "cheap" in prices
        assert "strong" in prices
        assert prices["cheap"].input_per_million == 1.00
        assert prices["cheap"].output_per_million == 2.00
        assert prices["strong"].input_per_million == 3.00
        assert prices["strong"].output_per_million == 6.00

        # Custom save
        custom_prices = {
            "cheap": TierPrice(0.50, 1.50),
            "strong": TierPrice(4.00, 8.00),
        }
        save_prices(custom_prices, prices_file)

        loaded_custom = load_prices(prices_file)
        assert loaded_custom["cheap"].input_per_million == 0.50
        assert loaded_custom["strong"].output_per_million == 8.00


def test_price_call_calculation():
    """Verify exact USD calculation for cheap and strong tiers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prices_file = Path(tmpdir) / "prices.json"
        save_prices(DEFAULT_PRICES, prices_file)

        # 100k in, 50k out on cheap ($1.00 / $2.00)
        # in: 0.10, out: 0.10 -> total 0.20
        cost_cheap = price_call("cheap", 100_000, 50_000, prices_path=prices_file)
        assert pytest.approx(cost_cheap, rel=1e-5) == 0.20

        # 100k in, 50k out on strong ($3.00 / $6.00)
        # in: 0.30, out: 0.30 -> total 0.60
        cost_strong = price_call("strong", 100_000, 50_000, prices_path=prices_file)
        assert pytest.approx(cost_strong, rel=1e-5) == 0.60


def test_estimate_prompt_tokens():
    """Verify 4-char token heuristic."""
    assert estimate_prompt_tokens("") == 0
    assert estimate_prompt_tokens("abc") == 1
    assert estimate_prompt_tokens("a" * 40) == 10


def test_record_model_call_prompt_cache_heuristic():
    """
    Verify record_model_call prices on max(prompt_eval_tokens, chars // 4)
    to handle Ollama prompt caching re-use.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_ledger.db"
        prices_path = Path(tmpdir) / "prices.json"
        init_db(db_path)
        save_prices(DEFAULT_PRICES, prices_path)

        # Case 1: Raw prompt_eval_tokens is large (500) while prompt length is small (100 chars -> 25 tokens)
        prompt_short = "short prompt " * 8  # ~104 chars -> 26 tokens
        res1 = record_model_call(
            model="qwen2.5:3b",
            prompt=prompt_short,
            response="Hello there",
            prompt_eval_tokens=500,
            output_tokens=100,
            tier="cheap",
            session_id="sess_cache_1",
            db_path=db_path,
            prices_path=prices_path,
        )
        assert res1["billable_input_tokens"] == 500
        assert res1["total_tokens"] == 600

        # Case 2: Prompt caching re-use: prompt_eval_tokens reported as only 10,
        # but prompt is 2000 chars (~500 tokens).
        # Billable input tokens must price on 500 tokens!
        prompt_long = "long context line repeat " * 80  # 2000 chars -> 500 tokens
        res2 = record_model_call(
            model="qwen2.5:3b",
            prompt=prompt_long,
            response="Processed cached context",
            prompt_eval_tokens=10,
            output_tokens=100,
            tier="cheap",
            session_id="sess_cache_2",
            db_path=db_path,
            prices_path=prices_path,
        )
        assert res2["raw_prompt_eval_tokens"] == 10
        assert res2["billable_input_tokens"] == 500
        assert res2["total_tokens"] == 600

        # Verify insertion into model_calls table
        conn = get_connection(db_path)
        cursor = conn.execute(
            "SELECT input_tokens, output_tokens, estimated_cost FROM model_calls WHERE session_id = 'sess_cache_2';"
        )
        row = cursor.fetchone()
        assert row is not None
        assert row["input_tokens"] == 500
        assert row["output_tokens"] == 100
        conn.close()


def test_detect_anomalies_pure_advisory():
    """
    Verify detect_anomalies is a pure advisory detector using mean + 2σ
    and does NOT mutate parameters or predict options.
    """
    # 1. Less than 2 samples -> No anomaly
    report_empty = detect_anomalies([])
    assert not report_empty.has_anomaly
    assert report_empty.status == "NORMAL"

    report_single = detect_anomalies([{"total_time": 1.0, "input_tokens": 50, "output_tokens": 50}])
    assert not report_single.has_anomaly

    # 2. Stable baseline of 5 interactions
    history = [
        {"total_time": 1.0, "input_tokens": 40, "output_tokens": 50},
        {"total_time": 1.1, "input_tokens": 42, "output_tokens": 48},
        {"total_time": 0.9, "input_tokens": 38, "output_tokens": 52},
        {"total_time": 1.0, "input_tokens": 41, "output_tokens": 49},
        {"total_time": 1.2, "input_tokens": 45, "output_tokens": 51},
    ]

    # Normal observation
    normal_obs = {"total_time": 1.05, "input_tokens": 42, "output_tokens": 50}
    report_normal = detect_anomalies(history, current=normal_obs)
    assert not report_normal.has_anomaly
    assert report_normal.anomaly_count == 0
    assert report_normal.status == "NORMAL"

    # Outlier observation: high latency (15s) and high output tokens (400)
    outlier_obs = {"total_time": 15.0, "input_tokens": 42, "output_tokens": 400}
    report_outlier = detect_anomalies(history, current=outlier_obs)
    assert report_outlier.has_anomaly
    assert report_outlier.latency_anomaly
    assert not report_outlier.input_token_anomaly
    assert report_outlier.output_token_anomaly
    assert report_outlier.anomaly_count == 2
    assert report_outlier.status == "ALERT"

    # Verify report is purely advisory
    assert "num_predict" not in dir(report_outlier)
    assert "num_predict" not in report_outlier.to_dict()


def test_cost_ledger_session_cost_aggregation():
    """Verify CostLedger helper tracks session cost."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "ledger_agg.db"
        prices_path = Path(tmpdir) / "prices.json"
        init_db(db_path)
        save_prices(DEFAULT_PRICES, prices_path)

        ledger = CostLedger(db_path=db_path)
        # Record 2 calls in sess_alpha
        record_model_call(
            model="qwen2.5:3b",
            prompt="Hi",
            response="Hello",
            prompt_eval_tokens=100_000,
            output_tokens=50_000,
            tier="cheap",
            session_id="sess_alpha",
            db_path=db_path,
            prices_path=prices_path,
        )
        record_model_call(
            model="qwen2.5:3b",
            prompt="Another",
            response="Response",
            prompt_eval_tokens=100_000,
            output_tokens=50_000,
            tier="cheap",
            session_id="sess_alpha",
            db_path=db_path,
            prices_path=prices_path,
        )

        total_cost = ledger.get_session_cost("sess_alpha")
        # 2 calls of $0.20 each = $0.40
        assert pytest.approx(total_cost, rel=1e-5) == 0.40
