"""Stage 2A unit tests: Kalshi V2 read-only parser, YES/NO complement normalization, fees, and Risk Gate cross-venue rules."""

from __future__ import annotations

import pytest

from backend.arbitrage.calculator import calculate_vwap_for_shares
from backend.risk_gate.engine import DeterministicRiskGate, RiskEvaluationRequest
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore
from backend.venues.kalshi import (
    calculate_kalshi_taker_fee,
    normalize_kalshi_orderbook,
    parse_kalshi_market,
)


def test_normalize_kalshi_orderbook_fp_derives_complement_asks_and_sorts():
    # Live Kalshi orderbook_fp returns yes_dollars and no_dollars bids sorted ascending
    raw_payload = {
        "orderbook_fp": {
            "yes_dollars": [
                ["0.0500", "200.00"],
                ["0.4200", "120.00"],
                ["0.4400", "30.00"],
            ],
            "no_dollars": [
                ["0.5000", "100.00"],
                ["0.5300", "60.00"],
                ["0.5400", "20.00"],
            ],
        }
    }

    yes_book, no_book, y_cnt, n_cnt = normalize_kalshi_orderbook(raw_payload)
    assert y_cnt == 3 and n_cnt == 3

    # YES bids DESC: 0.44, 0.42, 0.05
    assert [lvl.price for lvl in yes_book.bids] == [0.44, 0.42, 0.05]
    # YES asks ASC = 1.00 - NO bid: 0.46 (20), 0.47 (60), 0.50 (100)
    assert [(lvl.price, lvl.size) for lvl in yes_book.asks] == [
        (0.46, 20.0),
        (0.47, 60.0),
        (0.50, 100.0),
    ]
    assert yes_book.best_bid == pytest.approx(0.44)
    assert yes_book.best_ask == pytest.approx(0.46)

    # NO bids DESC: 0.54, 0.53, 0.50
    assert [lvl.price for lvl in no_book.bids] == [0.54, 0.53, 0.50]
    # NO asks ASC = 1.00 - YES bid: 0.56 (30), 0.58 (120), 0.95 (200)
    assert [(lvl.price, lvl.size) for lvl in no_book.asks] == [
        (0.56, 30.0),
        (0.58, 120.0),
        (0.95, 200.0),
    ]

    # Walk 50 YES shares: 20 @ 0.46 + 30 @ 0.47 = 9.20 + 14.10 = 23.30 -> VWAP 0.466
    fill = calculate_vwap_for_shares(yes_book, target_shares=50.0)
    assert fill.executable is True
    assert fill.vwap == pytest.approx(0.466)


def test_normalize_kalshi_orderbook_supports_legacy_cents():
    legacy_payload = {
        "orderbook": {
            "yes": [[40, 100], [45, 50]],
            "no": [[52, 80]],
        }
    }
    yes_book, no_book, _, _ = normalize_kalshi_orderbook(legacy_payload)
    assert yes_book.best_bid == pytest.approx(0.45)
    assert yes_book.best_ask == pytest.approx(0.48)  # 1.00 - 0.52
    assert no_book.best_ask == pytest.approx(0.55)   # 1.00 - 0.45


def test_kalshi_taker_fee_rounds_up_to_nearest_cent():
    # 0.07 * 100 * 0.50 * 0.50 = $1.75
    assert calculate_kalshi_taker_fee(100, 0.50) == pytest.approx(1.75)
    # 0.07 * 25 * 0.46 * 0.54 = 0.4347 -> ceil to $0.44
    assert calculate_kalshi_taker_fee(25, 0.46) == pytest.approx(0.44)
    assert calculate_kalshi_taker_fee(25, 0.46, fee_rate=0.0) == 0.0


def test_parse_kalshi_market_excludes_mve_parlays_and_closed_markets():
    valid = {
        "ticker": "KXFED-26DEC",
        "event_ticker": "KXFED-26",
        "title": "Will Fed cut in Dec?",
        "status": "active",
        "market_type": "binary",
        "yes_bid_dollars": "0.4200",
        "yes_ask_dollars": "0.4500",
        "no_bid_dollars": "0.5500",
        "no_ask_dollars": "0.5800",
        "volume_24h_fp": "1250.50",
        "open_interest_fp": "5000.00",
        "rules_primary": "Resolves YES per official FOMC statement.",
        "close_time": "2026-12-18T19:00:00Z",
        "expiration_time": "2026-12-18T19:00:00Z",
        "updated_time": "2026-11-12T23:00:00Z",
    }
    parsed = parse_kalshi_market(valid, series_ticker="KXFED")
    assert parsed is not None
    assert parsed.ticker == "KXFED-26DEC"
    assert parsed.volume_24h == pytest.approx(1250.50)

    parlay = dict(valid, mve_collection_ticker="KXMVE-1", mve_selected_legs=[{"ticker": "A"}])
    assert parse_kalshi_market(parlay) is None

    closed = dict(valid, status="closed")
    assert parse_kalshi_market(closed) is None


def test_risk_gate_enforces_2_percent_cross_venue_hurdle_and_dual_venue_caps():
    store = DuckDBStore(db_path=":memory:")
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())

    base = dict(
        venue="polymarket",
        secondary_venue="kalshi",
        arbitrage_type="cross_venue",
        strategy_tier="tier1",
        execution_mode="PAPER_EXECUTION",
        market_id="POLY-123<->KXFED-26DEC",
        requested_notional_usd=120.0,
        current_venue_exposure_usd=100.0,
        secondary_requested_notional_usd=130.0,
        secondary_venue_exposure_usd=200.0,
        daily_tier_loss_usd=0.0,
        net_edge_pct=2.05,
        liquidity_participation_pct=4.5,
        data_age_ms=400,
        leg_imbalance_ratio=1.2,
        executable=True,
        identical_settlement_verified=True,
        partial_fill_protection_enabled=True,
        ai_suggested_override=False,
    )

    ok = gate.evaluate(RiskEvaluationRequest(**base))
    assert ok.approved is True

    # 1.99% < 2.00% cross-venue threshold -> DENIED
    low_edge = gate.evaluate(RiskEvaluationRequest(**dict(base, net_edge_pct=1.99)))
    assert low_edge.approved is False
    assert "MIN_NET_EDGE" in low_edge.failed_rules

    # Kalshi venue cap $2,500 breach -> DENIED
    kalshi_cap = gate.evaluate(
        RiskEvaluationRequest(**dict(base, secondary_venue_exposure_usd=2450.0))
    )
    assert kalshi_cap.approved is False
    assert "MAX_VENUE_EXPOSURE" in kalshi_cap.failed_rules

    # Unverified settlement -> DENIED
    unverified = gate.evaluate(
        RiskEvaluationRequest(**dict(base, identical_settlement_verified=False))
    )
    assert unverified.approved is False
    assert "IDENTICAL_SETTLEMENT" in unverified.failed_rules

    # LIVE attempt still permanently locked
    live = gate.evaluate(RiskEvaluationRequest(**dict(base, execution_mode="LIVE")))
    assert live.approved is False
    assert "LIVE_EXECUTION_LOCK" in live.failed_rules
    store.close()
