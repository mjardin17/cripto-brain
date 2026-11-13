"""Cross-venue binary arbitrage math and Polymarket <-> Kalshi scanner."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from backend.arbitrage.calculator import (
    NormalizedOrderBook,
    calculate_polymarket_taker_fee,
    calculate_vwap_for_shares,
    compute_data_freshness,
)
from backend.arbitrage.matcher import (
    SettlementMatchAssessment,
    assess_contract_equivalence,
)
from backend.risk_gate.engine import DeterministicRiskGate, RiskEvaluationRequest
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import (
    KalshiBinaryMarket,
    KalshiReadOnlyClient,
    calculate_kalshi_taker_fee,
)
from backend.venues.polymarket import PolymarketBinaryMarket, PolymarketReadOnlyClient

CrossVenueDirection = Literal["POLY_YES_KALSHI_NO", "KALSHI_YES_POLY_NO"]


@dataclass(frozen=True)
class CrossVenueRouteEvaluation:
    direction: CrossVenueDirection
    leg1_venue: str
    leg1_outcome: Literal["YES", "NO"]
    leg2_venue: str
    leg2_outcome: Literal["YES", "NO"]
    executable: bool
    target_shares: float
    leg1_vwap: float | None
    leg2_vwap: float | None
    leg1_best_ask: float | None
    leg2_best_ask: float | None
    leg1_best_bid: float | None
    leg2_best_bid: float | None
    combined_raw_cost: float | None
    leg1_fee_usd: float
    leg2_fee_usd: float
    total_fees_usd: float
    slippage_buffer_usd: float
    total_executable_cost_per_pair: float | None
    leg1_notional_usd: float
    leg2_notional_usd: float
    total_executable_notional_usd: float
    guaranteed_payout_per_pair: float
    gross_edge_pct: float | None
    net_edge_pct: float | None
    leg1_near_depth_shares: float
    leg2_near_depth_shares: float
    liquidity_participation_pct: float
    leg_imbalance_ratio: float


def evaluate_cross_venue_direction(
    *,
    direction: CrossVenueDirection,
    leg1_venue: str,
    leg1_outcome: Literal["YES", "NO"],
    leg1_book: NormalizedOrderBook,
    leg2_venue: str,
    leg2_outcome: Literal["YES", "NO"],
    leg2_book: NormalizedOrderBook,
    target_shares: float,
    poly_fee_rate: float,
    poly_fees_enabled: bool,
    poly_fee_exponent: float = 1.0,
    kalshi_fee_rate: float = 0.07,
    slippage_buffer_bps: float = 15.0,
) -> CrossVenueRouteEvaluation:
    """Evaluate one cross-venue direction against sorted ask books and exact venue fees."""
    fill1 = calculate_vwap_for_shares(leg1_book, target_shares)
    fill2 = calculate_vwap_for_shares(leg2_book, target_shares)

    d1 = fill1.near_best_ask_shares
    d2 = fill2.near_best_ask_shares
    min_d = min(d1, d2)
    max_d = max(d1, d2)

    if min_d > 0.0:
        participation_pct = round((target_shares / min_d) * 100.0, 3)
        imbalance_ratio = round(max_d / min_d, 3)
    else:
        participation_pct = 100.0
        imbalance_ratio = 999.0

    both_exec = (
        fill1.executable
        and fill2.executable
        and fill1.vwap is not None
        and fill2.vwap is not None
    )
    if not both_exec:
        return CrossVenueRouteEvaluation(
            direction=direction,
            leg1_venue=leg1_venue,
            leg1_outcome=leg1_outcome,
            leg2_venue=leg2_venue,
            leg2_outcome=leg2_outcome,
            executable=False,
            target_shares=target_shares,
            leg1_vwap=fill1.vwap,
            leg2_vwap=fill2.vwap,
            leg1_best_ask=leg1_book.best_ask,
            leg2_best_ask=leg2_book.best_ask,
            leg1_best_bid=leg1_book.best_bid,
            leg2_best_bid=leg2_book.best_bid,
            combined_raw_cost=None,
            leg1_fee_usd=0.0,
            leg2_fee_usd=0.0,
            total_fees_usd=0.0,
            slippage_buffer_usd=0.0,
            total_executable_cost_per_pair=None,
            leg1_notional_usd=0.0,
            leg2_notional_usd=0.0,
            total_executable_notional_usd=0.0,
            guaranteed_payout_per_pair=1.0,
            gross_edge_pct=None,
            net_edge_pct=None,
            leg1_near_depth_shares=d1,
            leg2_near_depth_shares=d2,
            liquidity_participation_pct=participation_pct,
            leg_imbalance_ratio=imbalance_ratio,
        )

    assert fill1.vwap is not None and fill2.vwap is not None

    def _venue_fee(venue_name: str, vwap: float) -> float:
        if venue_name == "polymarket":
            return calculate_polymarket_taker_fee(
                target_shares, vwap, poly_fee_rate, poly_fees_enabled, exponent=poly_fee_exponent
            )
        return calculate_kalshi_taker_fee(target_shares, vwap, fee_rate=kalshi_fee_rate)

    fee1 = _venue_fee(leg1_venue, fill1.vwap)
    fee2 = _venue_fee(leg2_venue, fill2.vwap)
    total_fees = round(fee1 + fee2, 5)

    combined_raw = round(fill1.vwap + fill2.vwap, 6)
    raw_notional = target_shares * combined_raw
    slippage_buf = round(raw_notional * (slippage_buffer_bps / 10_000.0), 5)

    leg1_notional = round(target_shares * fill1.vwap + fee1 + (slippage_buf * 0.5), 5)
    leg2_notional = round(target_shares * fill2.vwap + fee2 + (slippage_buf * 0.5), 5)
    total_notional = round(raw_notional + total_fees + slippage_buf, 5)
    cost_per_pair = round(total_notional / target_shares, 6)

    gross_edge_pct = round((1.0 - combined_raw) * 100.0, 4)
    net_edge_pct = round((1.0 - cost_per_pair) * 100.0, 4)

    return CrossVenueRouteEvaluation(
        direction=direction,
        leg1_venue=leg1_venue,
        leg1_outcome=leg1_outcome,
        leg2_venue=leg2_venue,
        leg2_outcome=leg2_outcome,
        executable=True,
        target_shares=target_shares,
        leg1_vwap=fill1.vwap,
        leg2_vwap=fill2.vwap,
        leg1_best_ask=leg1_book.best_ask,
        leg2_best_ask=leg2_book.best_ask,
        leg1_best_bid=leg1_book.best_bid,
        leg2_best_bid=leg2_book.best_bid,
        combined_raw_cost=combined_raw,
        leg1_fee_usd=fee1,
        leg2_fee_usd=fee2,
        total_fees_usd=total_fees,
        slippage_buffer_usd=slippage_buf,
        total_executable_cost_per_pair=cost_per_pair,
        leg1_notional_usd=leg1_notional,
        leg2_notional_usd=leg2_notional,
        total_executable_notional_usd=total_notional,
        guaranteed_payout_per_pair=1.0,
        gross_edge_pct=gross_edge_pct,
        net_edge_pct=net_edge_pct,
        leg1_near_depth_shares=d1,
        leg2_near_depth_shares=d2,
        liquidity_participation_pct=participation_pct,
        leg_imbalance_ratio=imbalance_ratio,
    )


def evaluate_cross_venue_arbitrage(
    *,
    poly_yes_book: NormalizedOrderBook,
    poly_no_book: NormalizedOrderBook,
    kalshi_yes_book: NormalizedOrderBook,
    kalshi_no_book: NormalizedOrderBook,
    target_shares: float,
    poly_fee_rate: float,
    poly_fees_enabled: bool,
    poly_fee_exponent: float = 1.0,
    kalshi_fee_rate: float = 0.07,
    slippage_buffer_bps: float = 15.0,
) -> tuple[CrossVenueRouteEvaluation, CrossVenueRouteEvaluation, CrossVenueRouteEvaluation]:
    """Evaluate both Polymarket<->Kalshi directions and return (best_route, route_a, route_b)."""
    route_a = evaluate_cross_venue_direction(
        direction="POLY_YES_KALSHI_NO",
        leg1_venue="polymarket",
        leg1_outcome="YES",
        leg1_book=poly_yes_book,
        leg2_venue="kalshi",
        leg2_outcome="NO",
        leg2_book=kalshi_no_book,
        target_shares=target_shares,
        poly_fee_rate=poly_fee_rate,
        poly_fees_enabled=poly_fees_enabled,
        poly_fee_exponent=poly_fee_exponent,
        kalshi_fee_rate=kalshi_fee_rate,
        slippage_buffer_bps=slippage_buffer_bps,
    )
    route_b = evaluate_cross_venue_direction(
        direction="KALSHI_YES_POLY_NO",
        leg1_venue="kalshi",
        leg1_outcome="YES",
        leg1_book=kalshi_yes_book,
        leg2_venue="polymarket",
        leg2_outcome="NO",
        leg2_book=poly_no_book,
        target_shares=target_shares,
        poly_fee_rate=poly_fee_rate,
        poly_fees_enabled=poly_fees_enabled,
        poly_fee_exponent=poly_fee_exponent,
        kalshi_fee_rate=kalshi_fee_rate,
        slippage_buffer_bps=slippage_buffer_bps,
    )

    def _score(r: CrossVenueRouteEvaluation) -> float:
        if not r.executable or r.net_edge_pct is None:
            return -9999.0
        return r.net_edge_pct

    best = route_a if _score(route_a) >= _score(route_b) else route_b
    return best, route_a, route_b


class CrossVenueScanner:
    """Evaluates registered Polymarket<->Kalshi contract pairs, gates via RiskGate, and persists to DuckDB."""

    def __init__(
        self,
        store: DuckDBStore,
        risk_gate: DeterministicRiskGate,
        poly_client: PolymarketReadOnlyClient | None = None,
        kalshi_client: KalshiReadOnlyClient | None = None,
    ) -> None:
        self.store = store
        self.risk_gate = risk_gate
        self.poly_client = poly_client or PolymarketReadOnlyClient()
        self.kalshi_client = kalshi_client or KalshiReadOnlyClient()

    async def evaluate_registered_pair(
        self,
        *,
        pair: dict[str, Any],
        poly_market: PolymarketBinaryMarket,
        kalshi_market: KalshiBinaryMarket,
        target_shares: float = 25.0,
        slippage_buffer_bps: float = 15.0,
        scan_id: str | None = None,
        execution_mode: Literal["PAPER_RESEARCH", "PAPER_EXECUTION"] = "PAPER_RESEARCH",
    ) -> dict[str, Any]:
        active_scan_id = scan_id or f"XSCAN-{uuid.uuid4().hex[:10].upper()}"
        assessment: SettlementMatchAssessment = assess_contract_equivalence(
            poly_market=poly_market,
            kalshi_market=kalshi_market,
            resolution_source_verified=bool(pair.get("resolution_source_verified", False)),
            predicate_verified=bool(pair.get("predicate_verified", False)),
            polarity_inverted=bool(pair.get("polarity_inverted", False)),
            void_rules_compatible=bool(pair.get("void_rules_compatible", False)),
            allow_early_close_variance=bool(pair.get("allow_early_close_variance", False)),
        )

        (poly_yes, poly_no, kalshi_snap) = await asyncio.gather(
            self.poly_client.fetch_order_book(poly_market.yes_token_id),
            self.poly_client.fetch_order_book(poly_market.no_token_id),
            self.kalshi_client.fetch_order_book(kalshi_market.ticker),
        )

        now_ms = int(time.time() * 1000)
        poly_fresh = compute_data_freshness(
            now_ms=now_ms,
            yes_book_ts_ms=poly_yes.timestamp_ms,
            no_book_ts_ms=poly_no.timestamp_ms,
            yes_fetched_at_ms=poly_yes.fetched_at_ms,
            no_fetched_at_ms=poly_no.fetched_at_ms,
        )
        kalshi_fresh = compute_data_freshness(
            now_ms=now_ms,
            yes_book_ts_ms=kalshi_snap.timestamp_ms,
            no_book_ts_ms=kalshi_snap.timestamp_ms,
            yes_fetched_at_ms=kalshi_snap.fetched_at_ms,
            no_fetched_at_ms=kalshi_snap.fetched_at_ms,
        )
        if not poly_fresh.valid or not kalshi_fresh.valid:
            canonical_age_ms = -1
        else:
            canonical_age_ms = max(poly_fresh.data_age_ms, kalshi_fresh.data_age_ms)

        best, route_a, route_b = evaluate_cross_venue_arbitrage(
            poly_yes_book=poly_yes.book,
            poly_no_book=poly_no.book,
            kalshi_yes_book=kalshi_snap.yes_book,
            kalshi_no_book=kalshi_snap.no_book,
            target_shares=target_shares,
            poly_fee_rate=poly_market.fee_rate,
            poly_fees_enabled=poly_market.fees_enabled,
            poly_fee_exponent=poly_market.fee_exponent,
            slippage_buffer_bps=slippage_buffer_bps,
        )

        exposures = self.store.get_paper_venue_exposures()
        daily_loss = self.store.get_daily_tier1_simulated_loss_usd()
        leg1_exp = exposures.get(best.leg1_venue, 0.0)
        leg2_exp = exposures.get(best.leg2_venue, 0.0)

        gate_result = self.risk_gate.evaluate(
            RiskEvaluationRequest(
                venue=best.leg1_venue,
                secondary_venue=best.leg2_venue,
                arbitrage_type="cross_venue",
                strategy_tier="tier1",
                execution_mode=execution_mode,
                market_id=pair["pair_id"],
                requested_notional_usd=best.leg1_notional_usd if best.leg1_notional_usd > 0 else target_shares,
                current_venue_exposure_usd=leg1_exp,
                secondary_requested_notional_usd=best.leg2_notional_usd,
                secondary_venue_exposure_usd=leg2_exp,
                daily_tier_loss_usd=daily_loss,
                net_edge_pct=best.net_edge_pct,
                liquidity_participation_pct=best.liquidity_participation_pct,
                data_age_ms=canonical_age_ms,
                leg_imbalance_ratio=best.leg_imbalance_ratio,
                executable=best.executable,
                identical_settlement_verified=assessment.identical_settlement_verified,
                partial_fill_protection_enabled=True,
            )
        )

        opp_id = f"XOPP-{uuid.uuid4().hex[:10].upper()}"
        record = {
            "xopp_id": opp_id,
            "scan_id": active_scan_id,
            "pair_id": pair["pair_id"],
            "poly_market_id": poly_market.market_id,
            "kalshi_ticker": kalshi_market.ticker,
            "question": poly_market.question,
            "direction": best.direction,
            "leg1_venue": best.leg1_venue,
            "leg1_outcome": best.leg1_outcome,
            "leg2_venue": best.leg2_venue,
            "leg2_outcome": best.leg2_outcome,
            "target_shares": target_shares,
            "leg1_vwap": best.leg1_vwap,
            "leg2_vwap": best.leg2_vwap,
            "combined_raw_cost": best.combined_raw_cost,
            "total_fees_usd": best.total_fees_usd,
            "slippage_buffer_usd": best.slippage_buffer_usd,
            "total_executable_cost": best.total_executable_cost_per_pair,
            "gross_edge_pct": best.gross_edge_pct,
            "net_edge_pct": best.net_edge_pct,
            "liquidity_participation_pct": best.liquidity_participation_pct,
            "leg_imbalance_ratio": best.leg_imbalance_ratio,
            "executable": best.executable,
            "identical_settlement_verified": assessment.identical_settlement_verified,
            "settlement_status": assessment.status,
            "risk_gate_approved": gate_result.approved,
            "risk_gate_reason": gate_result.summary,
            "risk_audit_id": gate_result.audit_id,
            "data_age_ms": canonical_age_ms,
            "route_a_net_edge_pct": route_a.net_edge_pct,
            "route_b_net_edge_pct": route_b.net_edge_pct,
            "provenance": "LIVE",
            "evaluated_at": utc_now_iso(),
        }
        self.store.record_cross_venue_opportunity(record)
        return {
            **record,
            "best_route": best,
            "assessment": assessment,
            "poly_yes_book": poly_yes.book,
            "poly_no_book": poly_no.book,
            "kalshi_yes_book": kalshi_snap.yes_book,
            "kalshi_no_book": kalshi_snap.no_book,
            "gate_result": gate_result.model_dump(),
        }
