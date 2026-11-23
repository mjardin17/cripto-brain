"""Read-only Kalshi Trade API v2 market data adapter and YES/NO book normalizer.

Strictly unauthenticated public market-data ingestion only.
No Kalshi API keys, private RSA signing keys, or live order submission exist here.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from backend.arbitrage.calculator import NormalizedOrderBook, OrderBookLevel

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_HEADERS = {
    "User-Agent": "TheCertaintyStack/1.0 (+read-only-research)",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class KalshiBinaryMarket:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    subtitle: str
    yes_sub_title: str
    no_sub_title: str
    rules_primary: str
    rules_secondary: str
    open_time: str
    close_time: str
    expiration_time: str
    updated_time: str
    updated_at_ms: int
    can_close_early: bool
    yes_bid_dollars: float | None
    yes_ask_dollars: float | None
    no_bid_dollars: float | None
    no_ask_dollars: float | None
    volume_24h: float
    open_interest: float


@dataclass(frozen=True)
class KalshiBookSnapshot:
    ticker: str
    timestamp_ms: int
    fetched_at_ms: int
    yes_book: NormalizedOrderBook
    no_book: NormalizedOrderBook
    raw_yes_bids_count: int
    raw_no_bids_count: int


def _parse_iso_ms(iso_str: str | None) -> int:
    if not iso_str:
        return 0
    try:
        cleaned = iso_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _to_float_or_none(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        num = float(val)
        return num if math.isfinite(num) else None
    except (TypeError, ValueError):
        return None


def _parse_kalshi_bid_levels(
    raw_levels: list[Any], *, in_cents: bool
) -> dict[float, float]:
    """Aggregate valid Kalshi bid levels into {price_dollars: size}."""
    merged: dict[float, float] = {}
    for entry in raw_levels or []:
        try:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                raw_price, raw_size = entry[0], entry[1]
            elif isinstance(entry, dict):
                raw_price = entry.get("price_dollars", entry.get("price"))
                raw_size = entry.get("size", entry.get("quantity", entry.get("count")))
            else:
                continue
            price = float(raw_price)
            if in_cents:
                price = price / 100.0
            size = float(raw_size)
        except (TypeError, ValueError):
            continue

        if not (math.isfinite(price) and math.isfinite(size)):
            continue
        price = round(price, 6)
        if price <= 0.0 or price >= 1.0 or size <= 0.0:
            continue
        merged[price] = merged.get(price, 0.0) + size
    return merged


def normalize_kalshi_orderbook(
    raw_payload: dict[str, Any],
) -> tuple[NormalizedOrderBook, NormalizedOrderBook, int, int]:
    """Normalize Kalshi /orderbook payload into YES and NO NormalizedOrderBooks.

    Supports both modern `orderbook_fp` (`yes_dollars`/`no_dollars` decimal strings)
    and legacy `orderbook` (`yes`/`no` integer cents).

    In Kalshi binary markets, every resting NO bid at price p_no implies a resting
    YES ask at (1.00 - p_no), and every resting YES bid at p_yes implies a resting
    NO ask at (1.00 - p_yes).
    """
    if "orderbook_fp" in raw_payload and isinstance(raw_payload["orderbook_fp"], dict):
        ob = raw_payload["orderbook_fp"]
        raw_yes = ob.get("yes_dollars") or []
        raw_no = ob.get("no_dollars") or []
        in_cents = False
    elif "orderbook" in raw_payload and isinstance(raw_payload["orderbook"], dict):
        ob = raw_payload["orderbook"]
        if "yes_dollars" in ob or "no_dollars" in ob:
            raw_yes = ob.get("yes_dollars") or []
            raw_no = ob.get("no_dollars") or []
            in_cents = False
        else:
            raw_yes = ob.get("yes") or []
            raw_no = ob.get("no") or []
            in_cents = True
    else:
        raw_yes = raw_payload.get("yes_dollars") or []
        raw_no = raw_payload.get("no_dollars") or []
        in_cents = False

    yes_bid_map = _parse_kalshi_bid_levels(raw_yes, in_cents=in_cents)
    no_bid_map = _parse_kalshi_bid_levels(raw_no, in_cents=in_cents)

    yes_bids = tuple(
        OrderBookLevel(price=p, size=round(s, 6))
        for p, s in sorted(yes_bid_map.items(), key=lambda kv: kv[0], reverse=True)
    )
    no_bids = tuple(
        OrderBookLevel(price=p, size=round(s, 6))
        for p, s in sorted(no_bid_map.items(), key=lambda kv: kv[0], reverse=True)
    )

    # Complement symmetry: YES ask = 1.00 - NO bid; NO ask = 1.00 - YES bid
    yes_asks = tuple(
        OrderBookLevel(price=round(1.0 - p, 6), size=round(s, 6))
        for p, s in sorted(no_bid_map.items(), key=lambda kv: round(1.0 - kv[0], 6))
        if 0.0 < round(1.0 - p, 6) < 1.0
    )
    no_asks = tuple(
        OrderBookLevel(price=round(1.0 - p, 6), size=round(s, 6))
        for p, s in sorted(yes_bid_map.items(), key=lambda kv: round(1.0 - kv[0], 6))
        if 0.0 < round(1.0 - p, 6) < 1.0
    )

    return (
        NormalizedOrderBook(bids=yes_bids, asks=yes_asks),
        NormalizedOrderBook(bids=no_bids, asks=no_asks),
        len(raw_yes),
        len(raw_no),
    )


def calculate_kalshi_taker_fee(
    shares: float,
    price: float,
    fee_rate: float = 0.07,
) -> float:
    """Compute Kalshi quadratic taker fee rounded up to nearest cent:

    ceil(fee_rate * shares * p * (1 - p) * 100) / 100
    """
    if fee_rate <= 0.0 or shares <= 0.0 or not (0.0 < price < 1.0):
        return 0.0
    raw_dollars = fee_rate * shares * price * (1.0 - price)
    # Round up to nearest cent cleanly while guarding float epsilon
    cents = math.ceil(round(raw_dollars * 100.0, 9))
    return round(cents / 100.0, 4)


def parse_kalshi_market(
    raw: dict[str, Any], *, series_ticker: str = ""
) -> KalshiBinaryMarket | None:
    """Validate and extract a single non-combo binary Kalshi market."""
    if raw.get("market_type", "binary") != "binary":
        return None
    if raw.get("status") not in ("active", "open"):
        return None
    # Exclude multivariate / combo parlays which are non-atomic custom legs
    if raw.get("mve_collection_ticker") or raw.get("mve_selected_legs"):
        return None

    ticker = str(raw.get("ticker") or "").strip()
    if not ticker:
        return None

    updated_time = str(raw.get("updated_time") or "")
    return KalshiBinaryMarket(
        ticker=ticker,
        event_ticker=str(raw.get("event_ticker") or ""),
        series_ticker=series_ticker or str(raw.get("series_ticker") or ""),
        title=str(raw.get("title") or ticker),
        subtitle=str(raw.get("subtitle") or ""),
        yes_sub_title=str(raw.get("yes_sub_title") or "Yes"),
        no_sub_title=str(raw.get("no_sub_title") or "No"),
        rules_primary=str(raw.get("rules_primary") or ""),
        rules_secondary=str(raw.get("rules_secondary") or ""),
        open_time=str(raw.get("open_time") or ""),
        close_time=str(raw.get("close_time") or ""),
        expiration_time=str(raw.get("expiration_time") or ""),
        updated_time=updated_time,
        updated_at_ms=_parse_iso_ms(updated_time),
        can_close_early=bool(raw.get("can_close_early", False)),
        yes_bid_dollars=_to_float_or_none(raw.get("yes_bid_dollars")),
        yes_ask_dollars=_to_float_or_none(raw.get("yes_ask_dollars")),
        no_bid_dollars=_to_float_or_none(raw.get("no_bid_dollars")),
        no_ask_dollars=_to_float_or_none(raw.get("no_ask_dollars")),
        volume_24h=float(raw.get("volume_24h_fp") or raw.get("volume_24h") or 0.0),
        open_interest=float(raw.get("open_interest_fp") or raw.get("open_interest") or 0.0),
    )


class KalshiReadOnlyClient:
    """Async public read-only client for Kalshi Trade API v2."""

    def __init__(self, timeout_s: float = 8.0) -> None:
        self.timeout_s = timeout_s
        self.live_execution = False

    async def check_connectivity(self) -> dict[str, Any]:
        t0 = time.monotonic()
        reachable = False
        exchange_active: bool | None = None
        trading_active: bool | None = None
        error_msg: str | None = None

        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            try:
                resp = await client.get(f"{KALSHI_BASE_URL}/exchange/status")
                reachable = resp.status_code == 200
                if reachable:
                    payload = resp.json()
                    exchange_active = bool(payload.get("exchange_active", False))
                    trading_active = bool(payload.get("trading_active", False))
                else:
                    error_msg = f"HTTP {resp.status_code}"
            except Exception as exc:
                error_msg = str(exc)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "venue_id": "kalshi",
            "name": "Kalshi Trade API v2",
            "phase": "Phase 2",
            "mode": "READ_ONLY",
            "status": "ACTIVE_READ_ONLY" if reachable else "DEGRADED",
            "provenance": "LIVE" if reachable else "UNAVAILABLE",
            "reachable": reachable,
            "exchange_active": exchange_active,
            "trading_active": trading_active,
            "error": error_msg,
            "probe_latency_ms": latency_ms,
            "live_execution": False,
        }

    async def fetch_active_binary_markets(
        self, limit: int = 20
    ) -> list[KalshiBinaryMarket]:
        """Discover non-combo active binary Kalshi markets sorted by 24h volume."""
        params = {
            "limit": "40",
            "status": "open",
            "with_nested_markets": "true",
        }
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            resp = await client.get(f"{KALSHI_BASE_URL}/events", params=params)
            resp.raise_for_status()
            payload = resp.json()

        parsed_list: list[KalshiBinaryMarket] = []
        for ev in payload.get("events", []):
            series = str(ev.get("series_ticker") or "")
            for raw_mkt in ev.get("markets", []):
                parsed = parse_kalshi_market(raw_mkt, series_ticker=series)
                if parsed is not None:
                    parsed_list.append(parsed)

        parsed_list.sort(key=lambda m: m.volume_24h, reverse=True)
        return parsed_list[:limit]

    async def fetch_market_raw(self, ticker: str) -> dict[str, Any] | None:
        """Read-only fetch of raw Kalshi v2 market payload (including finalized/settled result fields)."""
        cleaned = (ticker or "").strip()
        if not cleaned or any(ch in cleaned for ch in ("/", "?", "#", "\\", "..")):
            raise ValueError("Invalid Kalshi market ticker")
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            resp = await client.get(f"{KALSHI_BASE_URL}/markets/{cleaned}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
            return payload if isinstance(payload, dict) else None

    async def fetch_market(self, ticker: str) -> KalshiBinaryMarket | None:
        payload = await self.fetch_market_raw(ticker)
        if payload is None:
            return None
        raw_mkt = payload.get("market") or payload
        return parse_kalshi_market(raw_mkt)

    async def fetch_order_book(
        self,
        ticker: str,
        *,
        depth: int = 20,
        book_timestamp_ms: int | None = None,
    ) -> KalshiBookSnapshot:
        """Fetch and normalize Kalshi order book into YES/NO books."""
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            resp = await client.get(
                f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook",
                params={"depth": str(depth)},
            )
            resp.raise_for_status()
            payload = resp.json()

        fetched_at_ms = int(time.time() * 1000)
        yes_book, no_book, yes_cnt, no_cnt = normalize_kalshi_orderbook(payload)
        book_ts_ms = (
            book_timestamp_ms
            if (book_timestamp_ms is not None and book_timestamp_ms > 0)
            else fetched_at_ms
        )

        return KalshiBookSnapshot(
            ticker=ticker,
            timestamp_ms=book_ts_ms,
            fetched_at_ms=fetched_at_ms,
            yes_book=yes_book,
            no_book=no_book,
            raw_yes_bids_count=yes_cnt,
            raw_no_bids_count=no_cnt,
        )
