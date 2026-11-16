"""Pure deterministic math for Polymarket binary YES/NO arbitrage evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class NormalizedOrderBook:
    bids: tuple[OrderBookLevel, ...]  # Sorted descending (best/highest bid first)
    asks: tuple[OrderBookLevel, ...]  # Sorted ascending (best/lowest ask first)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 6)

    @property
    def total_ask_shares(self) -> float:
        return round(sum(level.size for level in self.asks), 4)

    def near_best_ask_shares(self, price_band: float = 0.05) -> float:
        if not self.asks:
            return 0.0
        cutoff = self.asks[0].price + price_band
        return round(sum(level.size for level in self.asks if level.price <= cutoff), 4)


@dataclass(frozen=True)
class VwapFillResult:
    executable: bool
    target_shares: float
    filled_shares: float
    vwap: float | None
    best_ask: float | None
    worst_fill_price: float | None
    total_available_shares: float
    near_best_ask_shares: float
    slippage_per_share: float


@dataclass(frozen=True)
class BinaryArbitrageEvaluation:
    executable: bool
    target_shares: float
    yes_best_ask: float | None
    no_best_ask: float | None
    yes_best_bid: float | None
    no_best_bid: float | None
    yes_vwap: float | None
    no_vwap: float | None
    combined_raw_cost: float | None
    yes_taker_fee_usd: float
    no_taker_fee_usd: float
    taker_fees_usd: float
    slippage_buffer_usd: float
    total_executable_cost_per_pair: float | None
    total_executable_notional_usd: float
    guaranteed_payout_per_pair: float
    gross_edge_pct: float | None
    net_edge_pct: float | None
    yes_near_depth_shares: float
    no_near_depth_shares: float
    liquidity_participation_pct: float
    leg_imbalance_ratio: float
    data_age_ms: int


@dataclass(frozen=True)
class DataFreshness:
    valid: bool
    data_age_ms: int
    clob_age_ms: int
    fetch_age_ms: int


def compute_data_freshness(
    now_ms: int,
    yes_book_ts_ms: int,
    no_book_ts_ms: int,
    yes_fetched_at_ms: int,
    no_fetched_at_ms: int,
    max_future_skew_ms: int = 5_000,
) -> DataFreshness:
    """Compute one deterministic, fail-closed data age across both binary legs.

    - Missing/non-positive timestamps fail closed (data_age_ms = -1).
    - Future-skewed timestamps beyond tolerance fail closed (data_age_ms = -1).
    - Canonical data_age_ms is fetch_age_ms (time since the live book was
      actually retrieved). The CLOB/venue book `timestamp` field is the book's
      LAST-UPDATE time (verified live: an unchanged resting book on a quiet
      market can carry a timestamp minutes old while still being the current
      live book). Measuring staleness from clob_age_ms falsely denied every
      quiet-but-valid market at the Risk Gate; clob_age_ms is therefore
      reported for diagnostics only, and the fetch age governs freshness.
    """
    timestamps = (yes_book_ts_ms, no_book_ts_ms, yes_fetched_at_ms, no_fetched_at_ms)
    if now_ms <= 0 or any(ts <= 0 for ts in timestamps):
        return DataFreshness(valid=False, data_age_ms=-1, clob_age_ms=-1, fetch_age_ms=-1)

    oldest_book_ts = min(yes_book_ts_ms, no_book_ts_ms)
    newest_book_ts = max(yes_book_ts_ms, no_book_ts_ms)
    oldest_fetch_ts = min(yes_fetched_at_ms, no_fetched_at_ms)

    if (newest_book_ts - now_ms) > max_future_skew_ms:
        return DataFreshness(valid=False, data_age_ms=-1, clob_age_ms=-1, fetch_age_ms=-1)

    clob_age_ms = max(0, now_ms - oldest_book_ts)
    fetch_age_ms = max(0, now_ms - oldest_fetch_ts)
    # Freshness for gating = fetch age (see docstring). The venue book
    # timestamp only feeds diagnostics and the fail-closed future-skew check.
    return DataFreshness(
        valid=True,
        data_age_ms=fetch_age_ms,
        clob_age_ms=clob_age_ms,
        fetch_age_ms=fetch_age_ms,
    )


def _parse_levels(raw_levels: list[dict[str, Any]], descending: bool) -> tuple[OrderBookLevel, ...]:
    merged: dict[float, float] = {}
    for lvl in raw_levels or []:
        try:
            price = float(lvl["price"])
            size = float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(price) and math.isfinite(size)):
            continue
        if price <= 0.0 or price >= 1.0 or size <= 0.0:
            continue
        merged[price] = merged.get(price, 0.0) + size

    ordered = sorted(merged.items(), key=lambda kv: kv[0], reverse=descending)
    return tuple(OrderBookLevel(price=p, size=round(s, 6)) for p, s in ordered)


def normalize_clob_book(
    raw_bids: list[dict[str, Any]], raw_asks: list[dict[str, Any]]
) -> NormalizedOrderBook:
    """Sort Polymarket CLOB V2 book levels cleanly (bids DESC, asks ASC)."""
    return NormalizedOrderBook(
        bids=_parse_levels(raw_bids, descending=True),
        asks=_parse_levels(raw_asks, descending=False),
    )


def calculate_vwap_for_shares(
    book: NormalizedOrderBook, target_shares: float, price_band: float = 0.05
) -> VwapFillResult:
    """Walk the sorted ask book to compute executable VWAP for target_shares."""
    if not math.isfinite(target_shares) or target_shares <= 0.0:
        raise ValueError("target_shares must be a positive finite number")

    total_available = book.total_ask_shares
    near_depth = book.near_best_ask_shares(price_band=price_band)
    best_ask = book.best_ask

    if not book.asks:
        return VwapFillResult(
            executable=False,
            target_shares=target_shares,
            filled_shares=0.0,
            vwap=None,
            best_ask=None,
            worst_fill_price=None,
            total_available_shares=0.0,
            near_best_ask_shares=0.0,
            slippage_per_share=0.0,
        )

    remaining = target_shares
    total_cost = 0.0
    filled = 0.0
    worst_price = best_ask

    for level in book.asks:
        if remaining <= 1e-9:
            break
        take = min(level.size, remaining)
        total_cost += take * level.price
        filled += take
        remaining -= take
        worst_price = level.price

    executable = filled >= (target_shares - 1e-9)
    vwap = round(total_cost / filled, 6) if filled > 0 else None
    slippage = round((vwap - best_ask), 6) if (vwap is not None and best_ask is not None) else 0.0

    return VwapFillResult(
        executable=executable,
        target_shares=target_shares,
        filled_shares=round(filled, 6),
        vwap=vwap,
        best_ask=best_ask,
        worst_fill_price=worst_price,
        total_available_shares=total_available,
        near_best_ask_shares=near_depth,
        slippage_per_share=max(0.0, slippage),
    )


def calculate_polymarket_taker_fee(
    shares: float,
    price: float,
    fee_rate: float,
    fees_enabled: bool,
    exponent: float = 1.0,
) -> float:
    """Compute Polymarket CLOB taker fee using verified Gamma feeSchedule model.

    When exponent == 1.0 (standard Polymarket feeSchedule):
        fee = shares * fee_rate * p * (1 - p)
    For custom exponents:
        fee = shares * p * fee_rate * (p * (1 - p)) ** exponent
    Makers pay $0.00; Phase 1 evaluates immediate taker-side ask execution.
    """
    if not fees_enabled or fee_rate <= 0.0 or shares <= 0.0:
        return 0.0
    if not (0.0 < price < 1.0):
        return 0.0
    uncertainty = price * (1.0 - price)
    if abs(exponent - 1.0) < 1e-9:
        raw_fee = shares * fee_rate * uncertainty
    else:
        raw_fee = shares * price * fee_rate * (uncertainty**exponent)
    return round(max(0.0, raw_fee), 5)


def evaluate_binary_arbitrage(
    yes_book: NormalizedOrderBook,
    no_book: NormalizedOrderBook,
    target_shares: float,
    fee_rate: float,
    fees_enabled: bool,
    data_age_ms: int,
    fee_exponent: float = 1.0,
    slippage_buffer_bps: float = 15.0,
) -> BinaryArbitrageEvaluation:
    """Evaluate executable binary arbitrage (buy YES + buy NO vs $1.00 guaranteed payout)."""
    yes_fill = calculate_vwap_for_shares(yes_book, target_shares)
    no_fill = calculate_vwap_for_shares(no_book, target_shares)

    yes_near = yes_fill.near_best_ask_shares
    no_near = no_fill.near_best_ask_shares
    min_near = min(yes_near, no_near)
    max_near = max(yes_near, no_near)

    if min_near > 0.0:
        participation_pct = round((target_shares / min_near) * 100.0, 3)
        imbalance_ratio = round(max_near / min_near, 3)
    else:
        participation_pct = 100.0
        imbalance_ratio = 999.0

    both_executable = (
        yes_fill.executable
        and no_fill.executable
        and yes_fill.vwap is not None
        and no_fill.vwap is not None
    )

    if not both_executable:
        return BinaryArbitrageEvaluation(
            executable=False,
            target_shares=target_shares,
            yes_best_ask=yes_book.best_ask,
            no_best_ask=no_book.best_ask,
            yes_best_bid=yes_book.best_bid,
            no_best_bid=no_book.best_bid,
            yes_vwap=yes_fill.vwap,
            no_vwap=no_fill.vwap,
            combined_raw_cost=None,
            yes_taker_fee_usd=0.0,
            no_taker_fee_usd=0.0,
            taker_fees_usd=0.0,
            slippage_buffer_usd=0.0,
            total_executable_cost_per_pair=None,
            total_executable_notional_usd=0.0,
            guaranteed_payout_per_pair=1.0,
            gross_edge_pct=None,
            net_edge_pct=None,
            yes_near_depth_shares=yes_near,
            no_near_depth_shares=no_near,
            liquidity_participation_pct=participation_pct,
            leg_imbalance_ratio=imbalance_ratio,
            data_age_ms=data_age_ms,
        )

    assert yes_fill.vwap is not None and no_fill.vwap is not None
    combined_raw = round(yes_fill.vwap + no_fill.vwap, 6)
    yes_fee = calculate_polymarket_taker_fee(
        target_shares, yes_fill.vwap, fee_rate, fees_enabled, exponent=fee_exponent
    )
    no_fee = calculate_polymarket_taker_fee(
        target_shares, no_fill.vwap, fee_rate, fees_enabled, exponent=fee_exponent
    )
    taker_fees = round(yes_fee + no_fee, 5)

    raw_notional = target_shares * combined_raw
    slippage_buffer_usd = round(raw_notional * (slippage_buffer_bps / 10_000.0), 5)
    total_notional = round(raw_notional + taker_fees + slippage_buffer_usd, 5)
    cost_per_pair = round(total_notional / target_shares, 6)

    gross_edge_pct = round((1.0 - combined_raw) * 100.0, 4)
    net_edge_pct = round((1.0 - cost_per_pair) * 100.0, 4)

    return BinaryArbitrageEvaluation(
        executable=True,
        target_shares=target_shares,
        yes_best_ask=yes_book.best_ask,
        no_best_ask=no_book.best_ask,
        yes_best_bid=yes_book.best_bid,
        no_best_bid=no_book.best_bid,
        yes_vwap=yes_fill.vwap,
        no_vwap=no_fill.vwap,
        combined_raw_cost=combined_raw,
        yes_taker_fee_usd=yes_fee,
        no_taker_fee_usd=no_fee,
        taker_fees_usd=taker_fees,
        slippage_buffer_usd=slippage_buffer_usd,
        total_executable_cost_per_pair=cost_per_pair,
        total_executable_notional_usd=total_notional,
        guaranteed_payout_per_pair=1.0,
        gross_edge_pct=gross_edge_pct,
        net_edge_pct=net_edge_pct,
        yes_near_depth_shares=yes_near,
        no_near_depth_shares=no_near,
        liquidity_participation_pct=participation_pct,
        leg_imbalance_ratio=imbalance_ratio,
        data_age_ms=data_age_ms,
    )
