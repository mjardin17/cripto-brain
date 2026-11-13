"""Polymarket Tier 1 Binary Arbitrage Scanner & DuckDB Logger."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from backend.arbitrage.calculator import evaluate_binary_arbitrage
from backend.risk_gate.engine import DeterministicRiskGate, RiskEvaluationRequest
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.polymarket import PolymarketBinaryMarket, PolymarketReadOnlyClient


class PolymarketArbitrageScanner:
    """Fetches active Polymarket binary markets + CLOB V2 books, evaluates arbitrage, and gates via RiskGate."""

    def __init__(
        self,
        store: DuckDBStore,
        risk_gate: DeterministicRiskGate,
        client: PolymarketReadOnlyClient | None = None,
    ) -> None:
        self.store = store
        self.risk_gate = risk_gate
        self.client = client or PolymarketReadOnlyClient()

    async def scan(
        self,
        limit: int = 12,
        target_shares: float = 25.0,
        slippage_buffer_bps: float = 15.0,
    ) -> dict[str, Any]:
        scan_id = f"SCAN-{uuid.uuid4().hex[:10].upper()}"
        started_at = utc_now_iso()
        t0 = time.monotonic()

        try:
            markets = await self.client.fetch_active_binary_markets(limit=limit)
        except Exception as exc:
            self.store.add_memory(
                category="BLOCKER_ESCALATION",
                title="Polymarket Gamma API ingestion failed during scan",
                summary=f"Gamma discovery failed cleanly without fabricating data: {exc}",
                evidence={"scan_id": scan_id, "error": str(exc)},
                status="ESCALATED",
            )
            return {
                "scan_id": scan_id,
                "status": "ERROR",
                "error": f"Gamma API ingestion failed: {exc}",
                "markets_scanned": 0,
                "opportunities": [],
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }

        semaphore = asyncio.Semaphore(5)

        async def _process_market(market: PolymarketBinaryMarket) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    yes_snap, no_snap = await asyncio.gather(
                        self.client.fetch_order_book(market.yes_token_id),
                        self.client.fetch_order_book(market.no_token_id),
                    )
                except Exception as exc:
                    self.store.add_memory(
                        category="MARKET_DATA_ERROR",
                        title=f"CLOB V2 book fetch failed for market {market.market_id}",
                        summary=f"Skipped market cleanly: {exc}",
                        evidence={"market_id": market.market_id, "error": str(exc)},
                        status="LOGGED",
                    )
                    return None

            now_ms = int(time.time() * 1000)
            oldest_book_ts = min(yes_snap.timestamp_ms, no_snap.timestamp_ms)
            # Polymarket CLOB timestamps represent latest book update time;
            # measure data age relative to fetch receipt time if clock skew occurs,
            # while honoring stale timestamp rejection when > max_data_age_ms.
            clob_age_ms = max(0, now_ms - oldest_book_ts)
            # Also record fetch latency so live REST polls with older resting book updates
            # are reported transparently.
            fetch_age_ms = max(0, now_ms - min(yes_snap.fetched_at_ms, no_snap.fetched_at_ms))
            effective_age_ms = max(fetch_age_ms, min(clob_age_ms, 60_000) if clob_age_ms > 0 else fetch_age_ms)

            eval_res = evaluate_binary_arbitrage(
                yes_book=yes_snap.book,
                no_book=no_snap.book,
                target_shares=target_shares,
                fee_rate=market.fee_rate,
                fees_enabled=market.fees_enabled,
                data_age_ms=clob_age_ms,
                fee_exponent=market.fee_exponent,
                slippage_buffer_bps=slippage_buffer_bps,
            )

            ingested_at = utc_now_iso()
            snapshot_id = f"SNAP-{uuid.uuid4().hex[:10].upper()}"
            book_payload = {
                "yes": {
                    "bids": [{"price": l.price, "size": l.size} for l in yes_snap.book.bids[:15]],
                    "asks": [{"price": l.price, "size": l.size} for l in yes_snap.book.asks[:15]],
                    "min_order_size": yes_snap.min_order_size,
                    "tick_size": yes_snap.tick_size,
                    "timestamp_ms": yes_snap.timestamp_ms,
                },
                "no": {
                    "bids": [{"price": l.price, "size": l.size} for l in no_snap.book.bids[:15]],
                    "asks": [{"price": l.price, "size": l.size} for l in no_snap.book.asks[:15]],
                    "min_order_size": no_snap.min_order_size,
                    "tick_size": no_snap.tick_size,
                    "timestamp_ms": no_snap.timestamp_ms,
                },
                "evaluation": {
                    "target_shares": target_shares,
                    "yes_vwap": eval_res.yes_vwap,
                    "no_vwap": eval_res.no_vwap,
                    "combined_raw_cost": eval_res.combined_raw_cost,
                    "yes_taker_fee_usd": eval_res.yes_taker_fee_usd,
                    "no_taker_fee_usd": eval_res.no_taker_fee_usd,
                    "taker_fees_usd": eval_res.taker_fees_usd,
                    "slippage_buffer_usd": eval_res.slippage_buffer_usd,
                    "total_executable_cost_per_pair": eval_res.total_executable_cost_per_pair,
                    "gross_edge_pct": eval_res.gross_edge_pct,
                    "net_edge_pct": eval_res.net_edge_pct,
                    "fee_rate": market.fee_rate,
                    "fees_enabled": market.fees_enabled,
                    "fee_type": market.fee_type,
                    "clob_age_ms": clob_age_ms,
                    "fetch_age_ms": fetch_age_ms,
                },
            }

            self.store.record_snapshot(
                {
                    "snapshot_id": snapshot_id,
                    "scan_id": scan_id,
                    "market_id": market.market_id,
                    "condition_id": market.condition_id,
                    "question": market.question,
                    "slug": market.slug,
                    "yes_token_id": market.yes_token_id,
                    "no_token_id": market.no_token_id,
                    "yes_best_ask": eval_res.yes_best_ask,
                    "no_best_ask": eval_res.no_best_ask,
                    "yes_best_bid": eval_res.yes_best_bid,
                    "no_best_bid": eval_res.no_best_bid,
                    "yes_ask_depth_shares": yes_snap.book.total_ask_shares,
                    "no_ask_depth_shares": no_snap.book.total_ask_shares,
                    "fee_rate": market.fee_rate,
                    "fees_enabled": market.fees_enabled,
                    "neg_risk": market.neg_risk,
                    "clob_timestamp_ms": oldest_book_ts,
                    "book_payload": book_payload,
                    "ingested_at": ingested_at,
                }
            )

            # Evaluate through the independent fail-closed Risk Gate
            gate_result = self.risk_gate.evaluate(
                RiskEvaluationRequest(
                    venue="polymarket",
                    arbitrage_type="intra_venue",
                    strategy_tier="tier1",
                    execution_mode="PAPER_RESEARCH",
                    market_id=market.market_id,
                    requested_notional_usd=eval_res.total_executable_notional_usd
                    if eval_res.total_executable_notional_usd > 0
                    else target_shares,
                    current_venue_exposure_usd=0.0,
                    daily_tier_loss_usd=0.0,
                    net_edge_pct=eval_res.net_edge_pct,
                    liquidity_participation_pct=eval_res.liquidity_participation_pct,
                    data_age_ms=fetch_age_ms,
                    leg_imbalance_ratio=eval_res.leg_imbalance_ratio,
                    executable=eval_res.executable,
                    identical_settlement_verified=market.identical_settlement_verified,
                    partial_fill_protection_enabled=True,
                )
            )

            opp_id = f"OPP-{uuid.uuid4().hex[:10].upper()}"
            opp_record = {
                "opp_id": opp_id,
                "scan_id": scan_id,
                "market_id": market.market_id,
                "condition_id": market.condition_id,
                "question": market.question,
                "slug": market.slug,
                "venue": "polymarket",
                "target_shares": target_shares,
                "yes_vwap": eval_res.yes_vwap,
                "no_vwap": eval_res.no_vwap,
                "combined_raw_cost": eval_res.combined_raw_cost,
                "taker_fees_usd": eval_res.taker_fees_usd,
                "slippage_buffer_usd": eval_res.slippage_buffer_usd,
                "total_executable_cost": eval_res.total_executable_cost_per_pair,
                "guaranteed_payout": eval_res.guaranteed_payout_per_pair,
                "gross_edge_pct": eval_res.gross_edge_pct,
                "net_edge_pct": eval_res.net_edge_pct,
                "liquidity_participation_pct": eval_res.liquidity_participation_pct,
                "leg_imbalance_ratio": eval_res.leg_imbalance_ratio,
                "executable": eval_res.executable,
                "risk_gate_approved": gate_result.approved,
                "risk_gate_reason": gate_result.summary,
                "data_age_ms": fetch_age_ms,
                "evaluated_at": utc_now_iso(),
            }
            self.store.record_opportunity(opp_record)
            return opp_record

        results = await asyncio.gather(*[_process_market(m) for m in markets])
        opportunities = [r for r in results if r is not None]
        opportunities.sort(
            key=lambda o: o["net_edge_pct"] if o["net_edge_pct"] is not None else -999.0,
            reverse=True,
        )

        approved_count = sum(1 for o in opportunities if o["risk_gate_approved"])
        return {
            "scan_id": scan_id,
            "status": "COMPLETED",
            "started_at": started_at,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "markets_discovered": len(markets),
            "markets_scanned": len(opportunities),
            "approved_count": approved_count,
            "opportunities": opportunities,
        }
