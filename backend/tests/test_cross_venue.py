"""Stage 2B unit & integration tests: strict settlement matcher, cross-venue VWAP/fee math, and scanner persistence."""

from __future__ import annotations

import asyncio
import time
import pytest

from backend.arbitrage.calculator import normalize_clob_book
from backend.arbitrage.cross_venue import CrossVenueScanner, evaluate_cross_venue_arbitrage
from backend.arbitrage.matcher import assess_contract_equivalence
from backend.risk_gate.engine import DeterministicRiskGate
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore
from backend.venues.kalshi import KalshiBinaryMarket, KalshiBookSnapshot, normalize_kalshi_orderbook
from backend.venues.polymarket import PolymarketBinaryMarket, PolymarketBookSnapshot


def _sample_poly(**overrides) -> PolymarketBinaryMarket:
    base = dict(
        market_id="POLY-559651",
        condition_id="0xcond123",
        question="Will US CPI exceed 3.0% in Dec 2026?",
        slug="us-cpi-dec-2026",
        end_date="2026-12-20T14:00:00Z",
        yes_token_id="Y-TOK",
        no_token_id="N-TOK",
        fees_enabled=True,
        fee_type="economics",
        fee_rate=0.02,
        fee_exponent=1.0,
        neg_risk=False,
        neg_risk_other=False,
        volume_24hr=50000.0,
        liquidity_usd=25000.0,
        identical_settlement_verified=True,
    )
    base.update(overrides)
    return PolymarketBinaryMarket(**base)


def _sample_kalshi(**overrides) -> KalshiBinaryMarket:
    base = dict(
        ticker="KXCPI-26DEC-T30",
        event_ticker="KXCPI-26DEC",
        series_ticker="KXCPI",
        title="Will CPI exceed 3.0% in December 2026?",
        subtitle="",
        yes_sub_title="Above 3.0%",
        no_sub_title="3.0% or below",
        rules_primary="Resolves Yes if official BLS CPI exceeds 3.0%.",
        rules_secondary="",
        open_time="2026-11-01T14:00:00Z",
        close_time="2026-12-20T13:30:00Z",
        expiration_time="2026-12-20T13:30:00Z",
        updated_time="2026-11-12T23:00:00Z",
        updated_at_ms=1794553000000,
        can_close_early=False,
        yes_bid_dollars=0.45,
        yes_ask_dollars=0.48,
        no_bid_dollars=0.52,
        no_ask_dollars=0.55,
        volume_24h=12000.0,
        open_interest=40000.0,
    )
    base.update(overrides)
    return KalshiBinaryMarket(**base)


def test_settlement_matcher_fails_closed_unless_all_equivalence_checks_pass():
    poly = _sample_poly()
    kalshi = _sample_kalshi()

    # Without explicit source/predicate/void verification -> fails closed
    unverified = assess_contract_equivalence(poly_market=poly, kalshi_market=kalshi)
    assert unverified.identical_settlement_verified is False
    assert unverified.status == "UNVERIFIED_FAIL_CLOSED"

    # Full verification passes
    verified = assess_contract_equivalence(
        poly_market=poly,
        kalshi_market=kalshi,
        resolution_source_verified=True,
        predicate_verified=True,
        void_rules_compatible=True,
    )
    assert verified.identical_settlement_verified is True
    assert verified.status == "VERIFIED"
    assert verified.cutoff_delta_hours == pytest.approx(0.5)

    # Expiration mismatch (>24h) fails closed even if attested
    mismatch_dt = assess_contract_equivalence(
        poly_market=poly,
        kalshi_market=_sample_kalshi(expiration_time="2026-12-28T13:30:00Z"),
        resolution_source_verified=True,
        predicate_verified=True,
        void_rules_compatible=True,
    )
    assert mismatch_dt.identical_settlement_verified is False
    assert mismatch_dt.status == "MISMATCH_BLOCKED"

    # Polymarket negRiskOther catch-all fails closed
    neg_other = assess_contract_equivalence(
        poly_market=_sample_poly(neg_risk_other=True, identical_settlement_verified=False),
        kalshi_market=kalshi,
        resolution_source_verified=True,
        predicate_verified=True,
        void_rules_compatible=True,
    )
    assert neg_other.identical_settlement_verified is False
    assert neg_other.status == "MISMATCH_BLOCKED"


def test_cross_venue_arbitrage_picks_best_direction_with_exact_fees():
    # Polymarket: YES ask 0.42 (1000 sh), NO ask 0.60 (1000 sh)
    poly_yes = normalize_clob_book([{"price": "0.40", "size": "500"}], [{"price": "0.42", "size": "1000"}])
    poly_no = normalize_clob_book([{"price": "0.58", "size": "500"}], [{"price": "0.60", "size": "1000"}])

    # Kalshi: YES bid 0.48 (600 sh) -> NO ask 0.52 (600 sh); NO bid 0.48 (600 sh) -> YES ask 0.52 (600 sh)
    k_yes, k_no, _, _ = normalize_kalshi_orderbook({
        "orderbook_fp": {
            "yes_dollars": [["0.4800", "600.00"]],
            "no_dollars": [["0.4800", "600.00"]],
        }
    })

    # Route A: Poly YES (0.42) + Kalshi NO (0.52) = 0.94 raw (6.0% gross, ~3.6% net)
    # Route B: Kalshi YES (0.52) + Poly NO (0.60) = 1.12 raw (-12% gross)
    best, route_a, route_b = evaluate_cross_venue_arbitrage(
        poly_yes_book=poly_yes,
        poly_no_book=poly_no,
        kalshi_yes_book=k_yes,
        kalshi_no_book=k_no,
        target_shares=25.0,
        poly_fee_rate=0.02,
        poly_fees_enabled=True,
        kalshi_fee_rate=0.07,
        slippage_buffer_bps=15.0,
    )
    assert best.direction == "POLY_YES_KALSHI_NO"
    assert route_a.combined_raw_cost == pytest.approx(0.94)
    assert route_b.combined_raw_cost == pytest.approx(1.12)
    assert best.net_edge_pct is not None and best.net_edge_pct > 2.0
    assert best.liquidity_participation_pct == pytest.approx((25.0 / 600.0) * 100.0, rel=1e-3)


def test_cross_venue_scanner_persists_and_blocks_unverified_settlement():
    now_ms = int(time.time() * 1000)

    class _MockPoly:
        async def fetch_order_book(self, token_id: str):
            p = "0.42" if token_id == "Y-TOK" else "0.60"
            return PolymarketBookSnapshot(
                token_id=token_id,
                market_condition_id="0xcond123",
                timestamp_ms=now_ms - 300,
                fetched_at_ms=now_ms,
                min_order_size=1.0,
                tick_size=0.01,
                neg_risk=False,
                book=normalize_clob_book([{"price": "0.40", "size": "500"}], [{"price": p, "size": "1000"}]),
                raw_bids_count=1,
                raw_asks_count=1,
            )

    class _MockKalshi:
        async def fetch_order_book(self, ticker: str, depth: int = 20, book_timestamp_ms=None):
            ky, kn, _, _ = normalize_kalshi_orderbook({
                "orderbook_fp": {
                    "yes_dollars": [["0.4800", "600.00"]],
                    "no_dollars": [["0.4800", "600.00"]],
                }
            })
            return KalshiBookSnapshot(
                ticker=ticker,
                timestamp_ms=now_ms - 200,
                fetched_at_ms=now_ms,
                yes_book=ky,
                no_book=kn,
                raw_yes_bids_count=1,
                raw_no_bids_count=1,
            )

    store = DuckDBStore(db_path=":memory:")
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    scanner = CrossVenueScanner(
        store=store, risk_gate=gate, poly_client=_MockPoly(), kalshi_client=_MockKalshi()  # type: ignore[arg-type]
    )

    poly_mkt = _sample_poly()
    kalshi_mkt = _sample_kalshi()

    # Unverified pair -> high net edge (>2%) must STILL be DENIED fail-closed
    unverified_pair = {
        "pair_id": "XPAIR-UNVERIFIED",
        "poly_market_id": poly_mkt.market_id,
        "kalshi_ticker": kalshi_mkt.ticker,
        "resolution_source_verified": False,
        "predicate_verified": True,
        "void_rules_compatible": True,
    }
    res_denied = asyncio.run(
        scanner.evaluate_registered_pair(
            pair=unverified_pair, poly_market=poly_mkt, kalshi_market=kalshi_mkt, target_shares=25.0
        )
    )
    assert res_denied["net_edge_pct"] > 2.0
    assert res_denied["identical_settlement_verified"] is False
    assert res_denied["risk_gate_approved"] is False
    assert "IDENTICAL_SETTLEMENT" in res_denied["risk_gate_reason"]

    # Verified pair -> passes >= 2.0% hurdle and logs to DuckDB
    verified_pair = dict(unverified_pair, pair_id="XPAIR-VERIFIED", resolution_source_verified=True)
    res_ok = asyncio.run(
        scanner.evaluate_registered_pair(
            pair=verified_pair, poly_market=poly_mkt, kalshi_market=kalshi_mkt, target_shares=25.0
        )
    )
    assert res_ok["identical_settlement_verified"] is True
    assert res_ok["risk_gate_approved"] is True
    assert len(store.list_cross_venue_opportunities()) == 2
    store.close()
