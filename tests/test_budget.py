"""Tests for the ledger and the budget guard (windows + downgrade)."""
import json
import time

from cmd_council.budget import BudgetGuard, Ledger
from cmd_council.config import (
    AppConfig,
    ModeConfig,
    ModelRef,
    PriceEntry,
    ProviderConfig,
)
from cmd_council.pricing import PriceBook


def make_cfg(**budget_overrides) -> AppConfig:
    budget = {
        "monthly_credits": 30.0,
        "window_5h": 9.0,
        "window_7d": 18.0,
        "soft_stop_pct": 80,
        "hard_stop_pct": 95,
    }
    budget.update(budget_overrides)
    return AppConfig(
        provider=ProviderConfig(api_key="test"),
        default_mode="standard",
        modes={
            "standard": ModeConfig(
                advisors=[ModelRef(model="cheap-a"), ModelRef(model="cheap-b")],
                chairman=ModelRef(model="prem-chair"),
                review=True,
            ),
            "eco": ModeConfig(
                advisors=[ModelRef(model="cheap-a"), ModelRef(model="cheap-b")],
                chairman=ModelRef(model="cheap-chair"),
                review=False,
            ),
        },
        budget=budget,
        pricing_fallback={
            "cheap-a": PriceEntry(input=0.6, output=2.2),
            "cheap-b": PriceEntry(input=0.6, output=2.2),
            "cheap-chair": PriceEntry(input=1.0, output=5.0),
            "prem-chair": PriceEntry(input=2.0, output=10.0),
        },
    )


def make_guard(tmp_path, cfg=None) -> BudgetGuard:
    cfg = cfg or make_cfg()
    pb = PriceBook(
        cfg.pricing_fallback, cfg.credit_multipliers, cache_path=tmp_path / "m.json"
    )
    ledger = Ledger(tmp_path / "ledger.jsonl")
    return BudgetGuard(cfg, ledger, pb)


def write_entry(ledger: Ledger, cost: float, age_seconds: float) -> None:
    entry = {
        "ts": time.time() - age_seconds,
        "model": "x",
        "stage": "stage1",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost": cost,
        "session_id": "s",
    }
    with ledger.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_ledger_windows(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    write_entry(ledger, 1.0, age_seconds=60)            # inside all windows
    write_entry(ledger, 2.0, age_seconds=6 * 3600)      # outside 5h, inside 7d
    write_entry(ledger, 4.0, age_seconds=8 * 86400)     # outside 7d too
    assert abs(ledger.cost_since(5 * 3600) - 1.0) < 1e-9
    assert abs(ledger.cost_since(7 * 86400) - 3.0) < 1e-9


def test_estimate_mode_cost_orders_modes(tmp_path):
    guard = make_guard(tmp_path)
    standard = guard.estimate_mode_cost("standard")
    eco = guard.estimate_mode_cost("eco")
    assert 0 < eco < standard


def test_preflight_allows_when_fresh(tmp_path):
    guard = make_guard(tmp_path)
    d = guard.preflight("standard")
    assert d.allowed and d.mode == "standard" and not d.warnings


def test_preflight_downgrades_near_soft_cap(tmp_path):
    guard = make_guard(tmp_path)
    # Fill the 5h window to just under soft cap (80% of $9 = $7.2) such
    # that standard no longer fits but eco does.
    est_standard = guard.estimate_mode_cost("standard")
    write_entry(guard.ledger, 7.2 - est_standard / 2, age_seconds=60)
    d = guard.preflight("standard")
    assert d.allowed
    assert d.mode == "eco"
    assert d.downgraded
    assert any("downgraded" in w for w in d.warnings)


def test_preflight_hard_stop(tmp_path):
    guard = make_guard(tmp_path)
    write_entry(guard.ledger, 8.9, age_seconds=60)  # ~99% of the $9 5h cap
    d = guard.preflight("standard")
    assert not d.allowed
    assert "hard stop" in d.reason

    forced = guard.preflight("standard", force=True)
    assert forced.allowed
    assert forced.mode == "eco"  # forces the cheapest available mode
