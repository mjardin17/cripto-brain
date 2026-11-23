"""Read-only Polymarket Gamma + CLOB V2 market data adapter.

Strictly unauthenticated public market-data ingestion only.
No private keys, wallet signers, or live order submission exist here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.arbitrage.calculator import NormalizedOrderBook, normalize_clob_book

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_HEADERS = {
    "User-Agent": "TheCertaintyStack/1.0 (+read-only-research)",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class PolymarketBinaryMarket:
    market_id: str
    condition_id: str
    question: str
    slug: str
    end_date: str
    yes_token_id: str
    no_token_id: str
    fees_enabled: bool
    fee_type: str
    fee_rate: float
    fee_exponent: float
    neg_risk: bool
    neg_risk_other: bool
    volume_24hr: float
    liquidity_usd: float
    identical_settlement_verified: bool


@dataclass(frozen=True)
class PolymarketBookSnapshot:
    token_id: str
    market_condition_id: str
    timestamp_ms: int
    fetched_at_ms: int
    min_order_size: float
    tick_size: float
    neg_risk: bool
    book: NormalizedOrderBook
    raw_bids_count: int
    raw_asks_count: int


def parse_gamma_binary_market(raw: dict[str, Any]) -> PolymarketBinaryMarket | None:
    """Validate and extract a strict binary YES/NO Polymarket market."""
    if not raw.get("enableOrderBook") or not raw.get("acceptingOrders", True):
        return None

    try:
        outcomes_raw = raw.get("outcomes")
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        tokens_raw = raw.get("clobTokenIds")
        tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    except (TypeError, ValueError):
        return None

    if not isinstance(outcomes, list) or not isinstance(tokens, list):
        return None
    if len(outcomes) != 2 or len(tokens) != 2:
        return None

    outcome_map = {str(o).strip().lower(): str(t).strip() for o, t in zip(outcomes, tokens)}
    if "yes" not in outcome_map or "no" not in outcome_map:
        return None

    yes_token = outcome_map["yes"]
    no_token = outcome_map["no"]
    if not yes_token or not no_token or yes_token == no_token:
        return None

    fee_schedule = raw.get("feeSchedule") or {}
    fees_enabled = bool(raw.get("feesEnabled", False))
    fee_rate = float(fee_schedule.get("rate", 0.0) or 0.0)
    fee_exponent = float(fee_schedule.get("exponent", 1.0) or 1.0)
    neg_risk = bool(raw.get("negRisk", False))
    neg_risk_other = bool(raw.get("negRiskOther", False))
    condition_id = str(raw.get("conditionId") or "").strip()

    # Identical settlement on Polymarket single binary condition holds when
    # conditionId is present and it's not an unmapped negRiskOther catch-all.
    identical_settlement_verified = bool(condition_id) and not neg_risk_other

    return PolymarketBinaryMarket(
        market_id=str(raw.get("id") or condition_id),
        condition_id=condition_id,
        question=str(raw.get("question") or "Untitled Market"),
        slug=str(raw.get("slug") or ""),
        end_date=str(raw.get("endDate") or ""),
        yes_token_id=yes_token,
        no_token_id=no_token,
        fees_enabled=fees_enabled,
        fee_type=str(raw.get("feeType") or ("fee_free" if not fees_enabled else "standard")),
        fee_rate=fee_rate,
        fee_exponent=fee_exponent,
        neg_risk=neg_risk,
        neg_risk_other=neg_risk_other,
        volume_24hr=float(raw.get("volume24hr") or 0.0),
        liquidity_usd=float(raw.get("liquidityNum") or raw.get("liquidity") or 0.0),
        identical_settlement_verified=identical_settlement_verified,
    )


class PolymarketReadOnlyClient:
    """Async read-only client for Gamma and CLOB V2 public endpoints."""

    def __init__(self, timeout_s: float = 8.0) -> None:
        self.timeout_s = timeout_s

    async def check_connectivity(self) -> dict[str, Any]:
        t0 = time.monotonic()
        gamma_ok = False
        clob_ok = False
        clob_time = None
        gamma_error = None
        clob_error = None

        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            try:
                gr = await client.get(f"{GAMMA_BASE_URL}/markets", params={"active": "true", "limit": "1"})
                gamma_ok = gr.status_code == 200
                if not gamma_ok:
                    gamma_error = f"HTTP {gr.status_code}"
            except Exception as exc:
                gamma_error = str(exc)

            try:
                cr = await client.get(f"{CLOB_BASE_URL}/time")
                clob_ok = cr.status_code == 200
                if clob_ok:
                    clob_time = cr.text.strip()
                else:
                    clob_error = f"HTTP {cr.status_code}"
            except Exception as exc:
                clob_error = str(exc)

        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "gamma_reachable": gamma_ok,
            "clob_reachable": clob_ok,
            "clob_server_time": clob_time,
            "gamma_error": gamma_error,
            "clob_error": clob_error,
            "probe_latency_ms": latency_ms,
        }

    async def fetch_active_binary_markets(self, limit: int = 20) -> list[PolymarketBinaryMarket]:
        fetch_limit = max(limit * 2, 20)
        params = {
            "active": "true",
            "closed": "false",
            "enableOrderBook": "true",
            "order": "volume24hr",
            "ascending": "false",
            "limit": str(fetch_limit),
        }
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            resp = await client.get(f"{GAMMA_BASE_URL}/markets", params=params)
            resp.raise_for_status()
            rows = resp.json()

        markets: list[PolymarketBinaryMarket] = []
        for raw in rows:
            parsed = parse_gamma_binary_market(raw)
            if parsed is not None:
                markets.append(parsed)
            if len(markets) >= limit:
                break
        return markets

    async def fetch_market_raw(self, market_id: str) -> dict[str, Any] | None:
        """Read-only fetch of raw Gamma market payload (including closed/resolved settlement fields)."""
        cleaned = (market_id or "").strip()
        if not cleaned or any(ch in cleaned for ch in ("/", "?", "#", "\\", "..")):
            raise ValueError("Invalid Polymarket market_id")
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            resp = await client.get(f"{GAMMA_BASE_URL}/markets/{cleaned}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
            return payload if isinstance(payload, dict) else None

    async def fetch_market(self, market_id: str) -> PolymarketBinaryMarket | None:
        raw = await self.fetch_market_raw(market_id)
        if raw is None:
            return None
        return parse_gamma_binary_market(raw)

    async def fetch_order_book(self, token_id: str) -> PolymarketBookSnapshot:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s) as client:
            resp = await client.get(f"{CLOB_BASE_URL}/book", params={"token_id": token_id})
            resp.raise_for_status()
            payload = resp.json()

        fetched_at_ms = int(time.time() * 1000)
        raw_ts = payload.get("timestamp")
        try:
            book_ts_ms = int(raw_ts) if raw_ts is not None else 0
        except (TypeError, ValueError):
            book_ts_ms = 0

        raw_bids = payload.get("bids") or []
        raw_asks = payload.get("asks") or []
        normalized = normalize_clob_book(raw_bids, raw_asks)

        return PolymarketBookSnapshot(
            token_id=token_id,
            market_condition_id=str(payload.get("market") or ""),
            timestamp_ms=book_ts_ms,
            fetched_at_ms=fetched_at_ms,
            min_order_size=float(payload.get("min_order_size") or 1.0),
            tick_size=float(payload.get("tick_size") or 0.01),
            neg_risk=bool(payload.get("neg_risk", False)),
            book=normalized,
            raw_bids_count=len(raw_bids),
            raw_asks_count=len(raw_asks),
        )
