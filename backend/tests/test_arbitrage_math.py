"""Independent unit tests for Polymarket CLOB V2 book sorting, VWAP, fees, and binary arbitrage math."""

from __future__ import annotations

import pytest

from backend.arbitrage.calculator import (
    calculate_polymarket_taker_fee,
    calculate_vwap_for_shares,
    evaluate_binary_arbitrage,
    normalize_clob_book,
)


def test_normalize_clob_book_sorts_inverted_polymarket_arrays():
    # Live Polymarket CLOB V2 returns bids ascending and asks descending.
    raw_bids = [{"price": "0.10", "size": "50"}, {"price": "0.45", "size": "100"}]
    raw_asks = [{"price": "0.99", "size": "200"}, {"price": "0.47", "size": "80"}, {"price": "0.49", "size": "40"}]

    book = normalize_clob_book(raw_bids, raw_asks)
    assert book.best_bid == pytest.approx(0.45)
    assert book.best_ask == pytest.approx(0.47)
    assert [lvl.price for lvl in book.asks] == [0.47, 0.49, 0.99]
    assert [lvl.price for lvl in book.bids] == [0.45, 0.10]


def test_vwap_walks_multiple_ask_levels_accurately():
    book = normalize_clob_book(
        raw_bids=[],
        raw_asks=[
            {"price": "0.40", "size": "10"},
            {"price": "0.45", "size": "20"},
            {"price": "0.55", "size": "50"},
        ],
    )
    fill = calculate_vwap_for_shares(book, target_shares=25.0)
    # 10 * 0.40 + 15 * 0.45 = 4.00 + 6.75 = 10.75 / 25 = 0.43
    assert fill.executable is True
    assert fill.vwap == pytest.approx(0.43)
    assert fill.worst_fill_price == pytest.approx(0.45)
    assert fill.slippage_per_share == pytest.approx(0.03)


def test_polymarket_taker_fee_matches_verified_curve():
    # Politics market: rate=0.04, p=0.50, shares=1000 -> 1000 * 0.04 * 0.5 * 0.5 = $10.00
    fee = calculate_polymarket_taker_fee(shares=1000.0, price=0.50, fee_rate=0.04, fees_enabled=True)
    assert fee == pytest.approx(10.0)

    # Fee-free market (e.g., Geopolitics)
    zero_fee = calculate_polymarket_taker_fee(shares=1000.0, price=0.50, fee_rate=0.04, fees_enabled=False)
    assert zero_fee == 0.0


def test_binary_arbitrage_detects_profitable_and_unprofitable_pairs():
    # YES asks: 0.45 (500 shares), NO asks: 0.51 (500 shares) -> raw sum 0.96 (4% gross edge)
    yes_book = normalize_clob_book([], [{"price": "0.45", "size": "500"}])
    no_book = normalize_clob_book([], [{"price": "0.51", "size": "500"}])

    res = evaluate_binary_arbitrage(
        yes_book=yes_book,
        no_book=no_book,
        target_shares=25.0,
        fee_rate=0.04,
        fees_enabled=True,
        data_age_ms=250,
        slippage_buffer_bps=15.0,
    )
    assert res.executable is True
    assert res.combined_raw_cost == pytest.approx(0.96)
    assert res.gross_edge_pct == pytest.approx(4.0)
    # Fees at p=0.45 and p=0.51 with 0.04 rate are ~0.99% each; after fees + 15bps buffer, net edge ~ 1.86%
    assert res.net_edge_pct is not None
    assert 1.7 < res.net_edge_pct < 2.2
    assert res.liquidity_participation_pct == pytest.approx(5.0)


def test_binary_arbitrage_marks_insufficient_depth_non_executable():
    yes_book = normalize_clob_book([], [{"price": "0.40", "size": "5"}])
    no_book = normalize_clob_book([], [{"price": "0.50", "size": "100"}])

    res = evaluate_binary_arbitrage(
        yes_book=yes_book,
        no_book=no_book,
        target_shares=25.0,
        fee_rate=0.0,
        fees_enabled=False,
        data_age_ms=100,
    )
    assert res.executable is False
    assert res.net_edge_pct is None
