"""Deterministic cross-venue paper execution simulator.

Simulates sequential two-leg binary arbitrage execution across Polymarket and Kalshi:
- Mandatory fail-closed DeterministicRiskGate authorization (`PAPER_EXECUTION`) prior to any order
- Limit-capped ask ladder fills + realistic leg-1 partial fills
- Transit latency simulation + post-latency Leg 2 price drift / rejection checks
- Automatic bid-book unwinds on Leg 1 when Leg 2 fails or partially fills
- Full DuckDB persistence across paper_orders, paper_fills, paper_positions, paper_pnl_ledger
- Strict SIMULATED provenance; live_trading_enabled remains permanently False
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from backend.arbitrage.calculator import (
    NormalizedOrderBook,
    OrderBookLevel,
    calculate_polymarket_taker_fee,
    compute_data_freshness,
)
from backend.arbitrage.cross_venue import (
    CrossVenueDirection,
    evaluate_cross_venue_direction,
)
from backend.arbitrage.matcher import assess_contract_equivalence
from backend.risk_gate.engine import DeterministicRiskGate, RiskEvaluationRequest
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import (
    KalshiBinaryMarket,
    KalshiReadOnlyClient,
    calculate_kalshi_taker_fee,
)
from backend.venues.polymarket import PolymarketBinaryMarket, PolymarketReadOnlyClient

ExecutionScenario = Literal[
    "LIVE_BOOKS",
    "PARTIAL_FILL_LEG1",
    "SECOND_LEG_PRICE_DRIFT_UNWIND",
    "LEG2_REJECTED_UNWIND",
]


@dataclass(frozen=True)
class SimulatedFillOutcome:
    requested_shares: float
    filled_shares: float
    avg_price: float | None
    raw_notional_usd: float
    fee_usd: float
    exhausted_book_or_limit: bool


def _compute_venue_fee(
    *,
    venue: str,
    shares: float,
    price: float | None,
    poly_fee_rate: float,
    poly_fees_enabled: bool,
    poly_fee_exponent: float = 1.0,
    kalshi_fee_rate: float = 0.07,
) -> float:
    if shares <= 0.0 or price is None:
        return 0.0
    if venue.lower() == "polymarket":
        return calculate_polymarket_taker_fee(
            shares,
            price,
            poly_fee_rate,
            poly_fees_enabled,
            exponent=poly_fee_exponent,
        )
    return calculate_kalshi_taker_fee(shares, price, fee_rate=kalshi_fee_rate)


def simulate_ask_fill_up_to_limit(
    *,
    book: NormalizedOrderBook,
    target_shares: float,
    limit_price: float,
    venue: str,
    poly_fee_rate: float,
    poly_fees_enabled: bool,
    poly_fee_exponent: float = 1.0,
    kalshi_fee_rate: float = 0.07,
    max_fill_shares: float | None = None,
) -> SimulatedFillOutcome:
    """Walk sorted ascending asks and fill up to limit_price (and optional partial-fill cap)."""
    cap = target_shares if max_fill_shares is None else max(0.0, min(target_shares, max_fill_shares))
    remaining = cap
    filled = 0.0
    raw_cost = 0.0

    for lvl in book.asks:
        if remaining <= 1e-9:
            break
        if lvl.price > limit_price + 1e-9:
            break
        take = min(lvl.size, remaining)
        filled += take
        raw_cost += take * lvl.price
        remaining -= take

    filled = round(filled, 6)
    avg_price = round(raw_cost / filled, 6) if filled > 1e-9 else None
    fee_usd = _compute_venue_fee(
        venue=venue,
        shares=filled,
        price=avg_price,
        poly_fee_rate=poly_fee_rate,
        poly_fees_enabled=poly_fees_enabled,
        poly_fee_exponent=poly_fee_exponent,
        kalshi_fee_rate=kalshi_fee_rate,
    )
    return SimulatedFillOutcome(
        requested_shares=round(target_shares, 6),
        filled_shares=filled,
        avg_price=avg_price,
        raw_notional_usd=round(raw_cost, 5),
        fee_usd=fee_usd,
        exhausted_book_or_limit=filled < (target_shares - 1e-6),
    )


def simulate_bid_unwind(
    *,
    book: NormalizedOrderBook,
    shares_to_sell: float,
    venue: str,
    poly_fee_rate: float,
    poly_fees_enabled: bool,
    poly_fee_exponent: float = 1.0,
    kalshi_fee_rate: float = 0.07,
) -> SimulatedFillOutcome:
    """Walk sorted descending resting bids to unwind an unhedged Leg 1 position."""
    remaining = max(0.0, shares_to_sell)
    filled = 0.0
    raw_proceeds = 0.0

    for lvl in book.bids:
        if remaining <= 1e-9:
            break
        take = min(lvl.size, remaining)
        filled += take
        raw_proceeds += take * lvl.price
        remaining -= take

    filled = round(filled, 6)
    avg_price = round(raw_proceeds / filled, 6) if filled > 1e-9 else None
    fee_usd = _compute_venue_fee(
        venue=venue,
        shares=filled,
        price=avg_price,
        poly_fee_rate=poly_fee_rate,
        poly_fees_enabled=poly_fees_enabled,
        poly_fee_exponent=poly_fee_exponent,
        kalshi_fee_rate=kalshi_fee_rate,
    )
    return SimulatedFillOutcome(
        requested_shares=round(shares_to_sell, 6),
        filled_shares=filled,
        avg_price=avg_price,
        raw_notional_usd=round(raw_proceeds, 5),
        fee_usd=fee_usd,
        exhausted_book_or_limit=filled < (shares_to_sell - 1e-6),
    )


def _apply_adverse_drift_to_book(
    book: NormalizedOrderBook, *, ask_shift: float = 0.08, bid_haircut: float = 0.02
) -> NormalizedOrderBook:
    """Create a stressed post-latency book snapshot where asks move worse and bids soften."""
    shifted_asks = tuple(
        OrderBookLevel(price=round(min(0.99, lvl.price + ask_shift), 6), size=lvl.size)
        for lvl in book.asks
        if lvl.price + ask_shift < 1.0
    )
    shifted_bids = tuple(
        OrderBookLevel(price=round(max(0.01, lvl.price - bid_haircut), 6), size=lvl.size)
        for lvl in book.bids
        if lvl.price - bid_haircut > 0.0
    )
    return NormalizedOrderBook(bids=shifted_bids, asks=shifted_asks)


class PaperExecutionRequest(BaseModel):
    pair_id: str
    direction: CrossVenueDirection | None = None
    target_shares: float = Field(default=25.0, gt=0.0, le=500.0)
    slippage_buffer_bps: float = Field(default=15.0, ge=0.0, le=200.0)
    max_leg_slippage_bps: float = Field(default=50.0, ge=0.0, le=500.0)
    leg1_latency_ms: int = Field(default=85, ge=0, le=5000)
    leg2_latency_ms: int = Field(default=110, ge=0, le=5000)
    scenario: ExecutionScenario = "LIVE_BOOKS"
    partial_fill_ratio: float = Field(default=0.4, gt=0.0, lt=1.0)
    ai_suggested_override: bool = False


class PaperExecutionEngine:
    """Executes cross-venue paper simulations subject to fail-closed Risk Gate approval."""

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

    async def execute(
        self,
        req: PaperExecutionRequest,
        *,
        pair: dict[str, Any] | None = None,
        poly_market: PolymarketBinaryMarket | None = None,
        kalshi_market: KalshiBinaryMarket | None = None,
        post_latency_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        pair_record = pair or self.store.get_cross_venue_pair(req.pair_id)
        if not pair_record:
            raise ValueError(f"Unknown cross-venue pair_id '{req.pair_id}'")

        poly_mkt = poly_market or await self.poly_client.fetch_market(pair_record["poly_market_id"])
        kalshi_mkt = kalshi_market or await self.kalshi_client.fetch_market(pair_record["kalshi_ticker"])
        if poly_mkt is None or kalshi_mkt is None:
            raise ValueError("Unable to load live market metadata for one or both venues")

        assessment = assess_contract_equivalence(
            poly_market=poly_mkt,
            kalshi_market=kalshi_mkt,
            resolution_source_verified=bool(pair_record.get("resolution_source_verified", False)),
            predicate_verified=bool(pair_record.get("predicate_verified", False)),
            polarity_inverted=bool(pair_record.get("polarity_inverted", False)),
            void_rules_compatible=bool(pair_record.get("void_rules_compatible", False)),
            allow_early_close_variance=bool(pair_record.get("allow_early_close_variance", False)),
        )

        # Fetch pre-trade live books from both venues
        poly_yes, poly_no, kalshi_snap = await asyncio.gather(
            self.poly_client.fetch_order_book(poly_mkt.yes_token_id),
            self.poly_client.fetch_order_book(poly_mkt.no_token_id),
            self.kalshi_client.fetch_order_book(kalshi_mkt.ticker),
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
        canonical_age_ms = (
            max(poly_fresh.data_age_ms, kalshi_fresh.data_age_ms)
            if (poly_fresh.valid and kalshi_fresh.valid)
            else -1
        )

        chosen_direction: CrossVenueDirection = req.direction or "POLY_YES_KALSHI_NO"
        if req.direction is None:
            eval_a = evaluate_cross_venue_direction(
                direction="POLY_YES_KALSHI_NO",
                leg1_venue="polymarket",
                leg1_outcome="YES",
                leg1_book=poly_yes.book,
                leg2_venue="kalshi",
                leg2_outcome="NO",
                leg2_book=kalshi_snap.no_book,
                target_shares=req.target_shares,
                poly_fee_rate=poly_mkt.fee_rate,
                poly_fees_enabled=poly_mkt.fees_enabled,
                poly_fee_exponent=poly_mkt.fee_exponent,
                slippage_buffer_bps=req.slippage_buffer_bps,
            )
            eval_b = evaluate_cross_venue_direction(
                direction="KALSHI_YES_POLY_NO",
                leg1_venue="kalshi",
                leg1_outcome="YES",
                leg1_book=kalshi_snap.yes_book,
                leg2_venue="polymarket",
                leg2_outcome="NO",
                leg2_book=poly_no.book,
                target_shares=req.target_shares,
                poly_fee_rate=poly_mkt.fee_rate,
                poly_fees_enabled=poly_mkt.fees_enabled,
                poly_fee_exponent=poly_mkt.fee_exponent,
                slippage_buffer_bps=req.slippage_buffer_bps,
            )
            a_edge = eval_a.net_edge_pct if (eval_a.executable and eval_a.net_edge_pct is not None) else -9999.0
            b_edge = eval_b.net_edge_pct if (eval_b.executable and eval_b.net_edge_pct is not None) else -9999.0
            chosen_direction = "POLY_YES_KALSHI_NO" if a_edge >= b_edge else "KALSHI_YES_POLY_NO"

        if chosen_direction == "POLY_YES_KALSHI_NO":
            leg1_venue, leg1_outcome, leg1_ref = "polymarket", "YES", poly_mkt.market_id
            leg1_book, leg1_token_or_ticker = poly_yes.book, poly_mkt.yes_token_id
            leg2_venue, leg2_outcome, leg2_ref = "kalshi", "NO", kalshi_mkt.ticker
            leg2_book, leg2_token_or_ticker = kalshi_snap.no_book, kalshi_mkt.ticker
        else:
            leg1_venue, leg1_outcome, leg1_ref = "kalshi", "YES", kalshi_mkt.ticker
            leg1_book, leg1_token_or_ticker = kalshi_snap.yes_book, kalshi_mkt.ticker
            leg2_venue, leg2_outcome, leg2_ref = "polymarket", "NO", poly_mkt.market_id
            leg2_book, leg2_token_or_ticker = poly_no.book, poly_mkt.no_token_id

        route_eval = evaluate_cross_venue_direction(
            direction=chosen_direction,
            leg1_venue=leg1_venue,
            leg1_outcome=leg1_outcome,  # type: ignore[arg-type]
            leg1_book=leg1_book,
            leg2_venue=leg2_venue,
            leg2_outcome=leg2_outcome,  # type: ignore[arg-type]
            leg2_book=leg2_book,
            target_shares=req.target_shares,
            poly_fee_rate=poly_mkt.fee_rate,
            poly_fees_enabled=poly_mkt.fees_enabled,
            poly_fee_exponent=poly_mkt.fee_exponent,
            slippage_buffer_bps=req.slippage_buffer_bps,
        )

        exposures = self.store.get_paper_venue_exposures()
        daily_loss = self.store.get_daily_tier1_simulated_loss_usd()

        gate_result = self.risk_gate.evaluate(
            RiskEvaluationRequest(
                venue=leg1_venue,
                secondary_venue=leg2_venue,
                arbitrage_type="cross_venue",
                strategy_tier="tier1",
                execution_mode="PAPER_EXECUTION",
                market_id=pair_record["pair_id"],
                requested_notional_usd=route_eval.leg1_notional_usd
                if route_eval.leg1_notional_usd > 0
                else req.target_shares,
                current_venue_exposure_usd=exposures.get(leg1_venue, 0.0),
                secondary_requested_notional_usd=route_eval.leg2_notional_usd,
                secondary_venue_exposure_usd=exposures.get(leg2_venue, 0.0),
                daily_tier_loss_usd=daily_loss,
                net_edge_pct=route_eval.net_edge_pct,
                liquidity_participation_pct=route_eval.liquidity_participation_pct,
                data_age_ms=canonical_age_ms,
                leg_imbalance_ratio=route_eval.leg_imbalance_ratio,
                executable=route_eval.executable,
                identical_settlement_verified=assessment.identical_settlement_verified,
                partial_fill_protection_enabled=True,
                ai_suggested_override=req.ai_suggested_override,
            )
        )

        execution_id = f"PEXEC-{uuid.uuid4().hex[:10].upper()}"
        if not gate_result.approved or route_eval.leg1_vwap is None or route_eval.leg2_vwap is None:
            return {
                "execution_id": execution_id,
                "pair_id": pair_record["pair_id"],
                "direction": chosen_direction,
                "status": "REJECTED_BY_RISK_GATE",
                "approved": False,
                "provenance": "SIMULATED",
                "live_trading_enabled": False,
                "scenario": req.scenario,
                "gate_result": gate_result.model_dump(),
                "orders": [],
                "fills": [],
                "positions": [],
                "pnl_entry": None,
                "summary": self.store.get_paper_summary(),
            }

        # Pre-trade Risk Gate approved -> simulate sequential paper execution
        limit_1 = round(min(0.99, route_eval.leg1_vwap * (1.0 + req.max_leg_slippage_bps / 10_000.0)), 6)
        limit_2 = round(min(0.99, route_eval.leg2_vwap * (1.0 + req.max_leg_slippage_bps / 10_000.0)), 6)

        max_leg1_fill = (
            round(req.target_shares * req.partial_fill_ratio, 6)
            if req.scenario == "PARTIAL_FILL_LEG1"
            else None
        )
        leg1_fill = simulate_ask_fill_up_to_limit(
            book=leg1_book,
            target_shares=req.target_shares,
            limit_price=limit_1,
            venue=leg1_venue,
            poly_fee_rate=poly_mkt.fee_rate,
            poly_fees_enabled=poly_mkt.fees_enabled,
            poly_fee_exponent=poly_mkt.fee_exponent,
            max_fill_shares=max_leg1_fill,
        )

        orders_out: list[dict[str, Any]] = []
        fills_out: list[dict[str, Any]] = []
        positions_out: list[dict[str, Any]] = []

        now_leg1 = utc_now_iso()
        order1_id = f"PORD-{uuid.uuid4().hex[:10].upper()}"
        if leg1_fill.filled_shares <= 1e-6 or leg1_fill.avg_price is None:
            order1 = {
                "order_id": order1_id,
                "execution_id": execution_id,
                "pair_id": pair_record["pair_id"],
                "audit_id": gate_result.audit_id,
                "leg_number": 1,
                "venue": leg1_venue,
                "market_ref": leg1_ref,
                "side": "BUY",
                "outcome": leg1_outcome,
                "order_type": "LIMIT_IOC",
                "requested_shares": req.target_shares,
                "filled_shares": 0.0,
                "limit_price": limit_1,
                "avg_fill_price": None,
                "fees_usd": 0.0,
                "latency_ms": req.leg1_latency_ms,
                "status": "CANCELLED_NO_FILL",
                "reason": "Leg 1 ask depth exhausted below limit price",
                "provenance": "SIMULATED",
                "created_at": now_leg1,
            }
            self.store.record_paper_order(order1)
            orders_out.append(order1)
            return {
                "execution_id": execution_id,
                "pair_id": pair_record["pair_id"],
                "direction": chosen_direction,
                "status": "CANCELLED_LEG1_NO_FILL",
                "approved": True,
                "provenance": "SIMULATED",
                "live_trading_enabled": False,
                "scenario": req.scenario,
                "gate_result": gate_result.model_dump(),
                "orders": orders_out,
                "fills": [],
                "positions": [],
                "pnl_entry": None,
                "summary": self.store.get_paper_summary(),
            }

        leg1_partial = leg1_fill.filled_shares < (req.target_shares - 1e-6)
        order1 = {
            "order_id": order1_id,
            "execution_id": execution_id,
            "pair_id": pair_record["pair_id"],
            "audit_id": gate_result.audit_id,
            "leg_number": 1,
            "venue": leg1_venue,
            "market_ref": leg1_ref,
            "side": "BUY",
            "outcome": leg1_outcome,
            "order_type": "LIMIT_IOC",
            "requested_shares": req.target_shares,
            "filled_shares": leg1_fill.filled_shares,
            "limit_price": limit_1,
            "avg_fill_price": leg1_fill.avg_price,
            "fees_usd": leg1_fill.fee_usd,
            "latency_ms": req.leg1_latency_ms,
            "status": "PARTIALLY_FILLED" if leg1_partial else "FILLED",
            "reason": (
                f"Partial fill {leg1_fill.filled_shares:.2f}/{req.target_shares:.2f} shares; remainder cancelled"
                if leg1_partial
                else f"Filled {leg1_fill.filled_shares:.2f} shares within limit {limit_1:.4f}"
            ),
            "provenance": "SIMULATED",
            "created_at": now_leg1,
        }
        self.store.record_paper_order(order1)
        orders_out.append(order1)

        fill1 = {
            "fill_id": f"PFILL-{uuid.uuid4().hex[:10].upper()}",
            "execution_id": execution_id,
            "order_id": order1_id,
            "venue": leg1_venue,
            "market_ref": leg1_ref,
            "side": "BUY",
            "outcome": leg1_outcome,
            "shares": leg1_fill.filled_shares,
            "fill_price": leg1_fill.avg_price,
            "notional_usd": leg1_fill.raw_notional_usd,
            "fee_usd": leg1_fill.fee_usd,
            "is_unwind": False,
            "provenance": "SIMULATED",
            "filled_at": now_leg1,
        }
        self.store.record_paper_fill(fill1)
        fills_out.append(fill1)

        # Simulate transit latency before Leg 2 submission + re-read post-latency books
        if post_latency_hook is not None:
            await post_latency_hook()
        else:
            await asyncio.sleep(min(0.05, (req.leg1_latency_ms + req.leg2_latency_ms) / 1000.0))

        # Re-read live post-latency books on both venues
        if leg2_venue == "kalshi":
            k_post, p_post = await asyncio.gather(
                self.kalshi_client.fetch_order_book(leg2_token_or_ticker),
                self.poly_client.fetch_order_book(leg1_token_or_ticker),
            )
            leg2_post_book = k_post.no_book if leg2_outcome == "NO" else k_post.yes_book
            leg1_post_book = p_post.book
        else:
            p_post, k_post = await asyncio.gather(
                self.poly_client.fetch_order_book(leg2_token_or_ticker),
                self.kalshi_client.fetch_order_book(leg1_token_or_ticker),
            )
            leg2_post_book = p_post.book
            leg1_post_book = k_post.yes_book if leg1_outcome == "YES" else k_post.no_book

        if req.scenario == "SECOND_LEG_PRICE_DRIFT_UNWIND":
            leg2_post_book = _apply_adverse_drift_to_book(leg2_post_book, ask_shift=0.09, bid_haircut=0.02)
            leg1_post_book = _apply_adverse_drift_to_book(leg1_post_book, ask_shift=0.02, bid_haircut=0.03)

        q1 = leg1_fill.filled_shares
        now_leg2 = utc_now_iso()
        order2_id = f"PORD-{uuid.uuid4().hex[:10].upper()}"

        if req.scenario == "LEG2_REJECTED_UNWIND":
            leg2_fill = SimulatedFillOutcome(
                requested_shares=q1,
                filled_shares=0.0,
                avg_price=None,
                raw_notional_usd=0.0,
                fee_usd=0.0,
                exhausted_book_or_limit=True,
            )
            order2_status = "REJECTED_SECOND_LEG"
            order2_reason = "Second-leg venue rejected IOC order after transit latency"
        else:
            leg2_fill = simulate_ask_fill_up_to_limit(
                book=leg2_post_book,
                target_shares=q1,
                limit_price=limit_2,
                venue=leg2_venue,
                poly_fee_rate=poly_mkt.fee_rate,
                poly_fees_enabled=poly_mkt.fees_enabled,
                poly_fee_exponent=poly_mkt.fee_exponent,
            )
            if leg2_fill.filled_shares <= 1e-6:
                order2_status = "CANCELLED_LIMIT_BREACH"
                order2_reason = f"Leg 2 book moved above limit {limit_2:.4f} during {req.leg2_latency_ms}ms latency"
            elif leg2_fill.filled_shares < (q1 - 1e-6):
                order2_status = "PARTIALLY_FILLED"
                order2_reason = f"Leg 2 filled {leg2_fill.filled_shares:.2f}/{q1:.2f} shares within limit; remainder cancelled"
            else:
                order2_status = "FILLED"
                order2_reason = f"Filled {leg2_fill.filled_shares:.2f} matched shares within limit {limit_2:.4f}"

        order2 = {
            "order_id": order2_id,
            "execution_id": execution_id,
            "pair_id": pair_record["pair_id"],
            "audit_id": gate_result.audit_id,
            "leg_number": 2,
            "venue": leg2_venue,
            "market_ref": leg2_ref,
            "side": "BUY",
            "outcome": leg2_outcome,
            "order_type": "LIMIT_IOC",
            "requested_shares": q1,
            "filled_shares": leg2_fill.filled_shares,
            "limit_price": limit_2,
            "avg_fill_price": leg2_fill.avg_price,
            "fees_usd": leg2_fill.fee_usd,
            "latency_ms": req.leg2_latency_ms,
            "status": order2_status,
            "reason": order2_reason,
            "provenance": "SIMULATED",
            "created_at": now_leg2,
        }
        self.store.record_paper_order(order2)
        orders_out.append(order2)

        if leg2_fill.filled_shares > 1e-6 and leg2_fill.avg_price is not None:
            fill2 = {
                "fill_id": f"PFILL-{uuid.uuid4().hex[:10].upper()}",
                "execution_id": execution_id,
                "order_id": order2_id,
                "venue": leg2_venue,
                "market_ref": leg2_ref,
                "side": "BUY",
                "outcome": leg2_outcome,
                "shares": leg2_fill.filled_shares,
                "fill_price": leg2_fill.avg_price,
                "notional_usd": leg2_fill.raw_notional_usd,
                "fee_usd": leg2_fill.fee_usd,
                "is_unwind": False,
                "provenance": "SIMULATED",
                "filled_at": now_leg2,
            }
            self.store.record_paper_fill(fill2)
            fills_out.append(fill2)

        matched_shares = round(min(q1, leg2_fill.filled_shares), 6)
        unhedged_shares = round(max(0.0, q1 - matched_shares), 6)

        matched_leg1_fee = round(leg1_fill.fee_usd * (matched_shares / q1), 5) if q1 > 0 else 0.0
        matched_leg2_fee = leg2_fill.fee_usd
        matched_cost = 0.0
        locked_arb_edge_usd = 0.0

        if matched_shares > 1e-6 and leg2_fill.avg_price is not None:
            leg1_matched_notional = round(matched_shares * leg1_fill.avg_price + matched_leg1_fee, 4)
            leg2_matched_notional = round(matched_shares * leg2_fill.avg_price + matched_leg2_fee, 4)
            matched_cost = round(leg1_matched_notional + leg2_matched_notional, 4)
            locked_arb_edge_usd = round((matched_shares * 1.0) - matched_cost, 4)

            pos1 = {
                "position_id": f"PPOS-{uuid.uuid4().hex[:10].upper()}",
                "execution_id": execution_id,
                "pair_id": pair_record["pair_id"],
                "venue": leg1_venue,
                "market_ref": leg1_ref,
                "outcome": leg1_outcome,
                "shares": matched_shares,
                "entry_price": leg1_fill.avg_price,
                "notional_usd": leg1_matched_notional,
                "locked_payout_usd": round(matched_shares * 0.5, 4),
                "status": "OPEN_HEDGED",
                "provenance": "SIMULATED",
                "updated_at": now_leg2,
            }
            pos2 = {
                "position_id": f"PPOS-{uuid.uuid4().hex[:10].upper()}",
                "execution_id": execution_id,
                "pair_id": pair_record["pair_id"],
                "venue": leg2_venue,
                "market_ref": leg2_ref,
                "outcome": leg2_outcome,
                "shares": matched_shares,
                "entry_price": leg2_fill.avg_price,
                "notional_usd": leg2_matched_notional,
                "locked_payout_usd": round(matched_shares * 0.5, 4),
                "status": "OPEN_HEDGED",
                "provenance": "SIMULATED",
                "updated_at": now_leg2,
            }
            self.store.record_paper_position(pos1)
            self.store.record_paper_position(pos2)
            positions_out.extend([pos1, pos2])

        # Unwind any unhedged Leg 1 exposure against resting Leg 1 bids
        unwound_shares = 0.0
        unwind_realized_pnl = 0.0
        unwind_fee = 0.0
        residual_unhedged = unhedged_shares

        if unhedged_shares > 1e-6:
            unwind = simulate_bid_unwind(
                book=leg1_post_book,
                shares_to_sell=unhedged_shares,
                venue=leg1_venue,
                poly_fee_rate=poly_mkt.fee_rate,
                poly_fees_enabled=poly_mkt.fees_enabled,
                poly_fee_exponent=poly_mkt.fee_exponent,
            )
            unwound_shares = unwind.filled_shares
            unwind_fee = unwind.fee_usd
            residual_unhedged = round(max(0.0, unhedged_shares - unwound_shares), 6)
            now_unwind = utc_now_iso()
            order3_id = f"PORD-{uuid.uuid4().hex[:10].upper()}"
            best_bid_floor = leg1_post_book.bids[-1].price if leg1_post_book.bids else 0.01

            order3 = {
                "order_id": order3_id,
                "execution_id": execution_id,
                "pair_id": pair_record["pair_id"],
                "audit_id": gate_result.audit_id,
                "leg_number": 3,
                "venue": leg1_venue,
                "market_ref": leg1_ref,
                "side": "SELL",
                "outcome": leg1_outcome,
                "order_type": "MARKET_UNWIND",
                "requested_shares": unhedged_shares,
                "filled_shares": unwound_shares,
                "limit_price": best_bid_floor,
                "avg_fill_price": unwind.avg_price,
                "fees_usd": unwind_fee,
                "latency_ms": req.leg1_latency_ms,
                "status": (
                    "UNWIND_FILLED"
                    if residual_unhedged <= 1e-6
                    else ("UNWIND_PARTIAL" if unwound_shares > 1e-6 else "UNWIND_FAILED_NO_BIDS")
                ),
                "reason": (
                    f"Unwound {unwound_shares:.2f}/{unhedged_shares:.2f} unhedged Leg 1 shares into resting bids"
                    if unwound_shares > 1e-6
                    else "Bid depth empty during emergency unwind"
                ),
                "provenance": "SIMULATED",
                "created_at": now_unwind,
            }
            self.store.record_paper_order(order3)
            orders_out.append(order3)

            if unwound_shares > 1e-6 and unwind.avg_price is not None:
                entry_fee_unwound = round(leg1_fill.fee_usd * (unwound_shares / q1), 5)
                entry_cost_unwound = unwound_shares * leg1_fill.avg_price + entry_fee_unwound
                net_proceeds = unwind.raw_notional_usd - unwind_fee
                unwind_realized_pnl = round(net_proceeds - entry_cost_unwound, 4)

                fill3 = {
                    "fill_id": f"PFILL-{uuid.uuid4().hex[:10].upper()}",
                    "execution_id": execution_id,
                    "order_id": order3_id,
                    "venue": leg1_venue,
                    "market_ref": leg1_ref,
                    "side": "SELL",
                    "outcome": leg1_outcome,
                    "shares": unwound_shares,
                    "fill_price": unwind.avg_price,
                    "notional_usd": unwind.raw_notional_usd,
                    "fee_usd": unwind_fee,
                    "is_unwind": True,
                    "provenance": "SIMULATED",
                    "filled_at": now_unwind,
                }
                self.store.record_paper_fill(fill3)
                fills_out.append(fill3)

                pos_unwound = {
                    "position_id": f"PPOS-{uuid.uuid4().hex[:10].upper()}",
                    "execution_id": execution_id,
                    "pair_id": pair_record["pair_id"],
                    "venue": leg1_venue,
                    "market_ref": leg1_ref,
                    "outcome": leg1_outcome,
                    "shares": unwound_shares,
                    "entry_price": leg1_fill.avg_price,
                    "notional_usd": round(entry_cost_unwound, 4),
                    "locked_payout_usd": 0.0,
                    "status": "UNWOUND",
                    "provenance": "SIMULATED",
                    "updated_at": now_unwind,
                }
                self.store.record_paper_position(pos_unwound)
                positions_out.append(pos_unwound)

            if residual_unhedged > 1e-6:
                res_fee = round(leg1_fill.fee_usd * (residual_unhedged / q1), 5)
                pos_open_unhedged = {
                    "position_id": f"PPOS-{uuid.uuid4().hex[:10].upper()}",
                    "execution_id": execution_id,
                    "pair_id": pair_record["pair_id"],
                    "venue": leg1_venue,
                    "market_ref": leg1_ref,
                    "outcome": leg1_outcome,
                    "shares": residual_unhedged,
                    "entry_price": leg1_fill.avg_price,
                    "notional_usd": round(residual_unhedged * leg1_fill.avg_price + res_fee, 4),
                    "locked_payout_usd": 0.0,
                    "status": "OPEN_UNHEDGED",
                    "provenance": "SIMULATED",
                    "updated_at": now_unwind,
                }
                self.store.record_paper_position(pos_open_unhedged)
                positions_out.append(pos_open_unhedged)

        total_fees = round(leg1_fill.fee_usd + leg2_fill.fee_usd + unwind_fee, 5)
        total_gross_cost = round(leg1_fill.raw_notional_usd + leg2_fill.raw_notional_usd, 4)

        if matched_shares >= (req.target_shares - 1e-6):
            outcome_status = "HEDGED_FULL_FILL"
        elif matched_shares > 1e-6 and leg1_partial and unhedged_shares <= 1e-6:
            outcome_status = "HEDGED_PARTIAL_LEG1"
        elif matched_shares > 1e-6 and unwound_shares > 1e-6:
            outcome_status = "PARTIAL_HEDGE_WITH_UNWIND"
        elif unwound_shares > 1e-6 and residual_unhedged <= 1e-6:
            outcome_status = "UNWOUND_SECOND_LEG_FAILURE"
        else:
            outcome_status = "UNHEDGED_RESIDUAL_RISK"

        pnl_entry = {
            "ledger_id": f"PPNL-{uuid.uuid4().hex[:10].upper()}",
            "execution_id": execution_id,
            "pair_id": pair_record["pair_id"],
            "event_type": "CROSS_VENUE_PAPER_EXECUTION",
            "outcome_status": outcome_status,
            "matched_shares": matched_shares,
            "unwound_shares": unwound_shares,
            "gross_cost_usd": total_gross_cost,
            "fees_usd": total_fees,
            "realized_pnl_usd": unwind_realized_pnl,
            "locked_arb_edge_usd": locked_arb_edge_usd,
            "notes": (
                f"{outcome_status}: matched={matched_shares:.2f}, unwound={unwound_shares:.2f}, "
                f"realized_pnl=${unwind_realized_pnl:.4f}, locked_carry_pending_settlement=${locked_arb_edge_usd:.4f}"
            ),
            "provenance": "SIMULATED",
            "recorded_at": utc_now_iso(),
        }
        self.store.record_paper_pnl(pnl_entry)

        return {
            "execution_id": execution_id,
            "pair_id": pair_record["pair_id"],
            "direction": chosen_direction,
            "status": outcome_status,
            "approved": True,
            "provenance": "SIMULATED",
            "live_trading_enabled": False,
            "scenario": req.scenario,
            "gate_result": gate_result.model_dump(),
            "orders": orders_out,
            "fills": fills_out,
            "positions": positions_out,
            "pnl_entry": pnl_entry,
            "summary": self.store.get_paper_summary(),
        }
