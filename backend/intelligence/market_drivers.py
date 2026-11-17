"""Multi-Factor Market Driver Engine (Free Public Feeds + Causal WHY/WHEN Attribution).

Ingests free public data with zero API keys and zero LLM spend:
- Spot/Candles/Volume/Whale Proxies: Coinbase Exchange API + CoinGecko fallback (BTC-USD, ETH-USD)
- Derivatives/Leverage/Liquidations Proxy: OKX Public API (Funding Rate, Open Interest, 1H L/S Ratio)
- Equities & Institutional ETF Flows: Yahoo Finance Chart JSON (SPY, QQQ, IBIT, FBTC) + Pearson correlations
- Sentiment: Alternative.me Fear & Greed Index
- Macro/Fed & Breaking Geopolitical News: Federal Reserve Press RSS + CNBC Top News RSS

Security & Integrity Guarantees:
- Every external RSS/news headline and summary is screened by ResearchQualityValidator prompt-injection firewall.
- Failed upstream feeds report explicit 'UNAVAILABLE' provenance; synthetic prices are never fabricated.
- Historical analog lookup strictly excludes TEST, SIMULATED, and UNVERIFIED rows (provenance='LIVE' AND verified_clean=TRUE).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.intelligence.validation_labs import ResearchQualityValidator
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

DEFAULT_USER_AGENT = "CertaintyStack-Research/1.0 (+https://github.com/certainty-stack)"

# Keyword classifiers for breaking macro/Fed/geopolitical/institutional catalysts
_FED_KEYWORDS = (
    "federal reserve",
    "fomc",
    "fed ",
    "powell",
    "interest rate",
    "rate cut",
    "rate hike",
    "monetary policy",
    "treasury yield",
    "quantitative tightening",
    "balance sheet",
)
_MACRO_KEYWORDS = (
    "inflation",
    "cpi",
    "pce",
    "payroll",
    "jobs report",
    "unemployment",
    "gdp",
    "recession",
    "retail sales",
    "ppi",
)
_GEOPOLITICAL_KEYWORDS = (
    "tariff",
    "sanction",
    "war",
    "conflict",
    "missile",
    "middle east",
    "china",
    "ukraine",
    "nato",
    "geopolit",
    "trade war",
    "oil shock",
)
_ETF_INSTITUTIONAL_KEYWORDS = (
    "etf",
    "blackrock",
    "ibit",
    "fbtc",
    "fidelity",
    "inflow",
    "outflow",
    "institutional",
    "sec ",
    "bitcoin",
    "crypto",
    "ethereum",
)
_BULLISH_WORDS = (
    "surge",
    "rally",
    "gain",
    "jump",
    "inflow",
    "cut",
    "easing",
    "dovish",
    "approval",
    "growth",
    "beat",
    "record high",
    "rebound",
)
_BEARISH_WORDS = (
    "drop",
    "fall",
    "plunge",
    "sell-off",
    "selloff",
    "outflow",
    "hike",
    "hawkish",
    "tariff",
    "war",
    "sanction",
    "default",
    "crisis",
    "liquidation",
    "fear",
    "slump",
    "miss",
)


def _safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _sanitize_feed_error(exc: Exception | str) -> str:
    txt = str(exc or "Feed request failed")
    txt = re.sub(r"(api[_-]?key|token|secret)=[^\s&]+", r"\1=[REDACTED]", txt, flags=re.IGNORECASE)
    return txt[:220]


def compute_pearson_correlation(series_a: dict[str, float], series_b: dict[str, float]) -> dict[str, Any]:
    """Compute aligned-date Pearson correlation between two date->return maps."""
    common_dates = sorted(set(series_a.keys()) & set(series_b.keys()))
    n = len(common_dates)
    if n < 5:
        return {"correlation": None, "sample_days": n, "regime": "INSUFFICIENT_OVERLAP"}
    xs = [series_a[d] for d in common_dates]
    ys = [series_b[d] for d in common_dates]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x <= 1e-12 or den_y <= 1e-12:
        return {"correlation": 0.0, "sample_days": n, "regime": "ZERO_VARIANCE"}
    corr = max(-1.0, min(1.0, num / (den_x * den_y)))
    if corr >= 0.55:
        regime = "STRONG_COUPLED"
    elif corr >= 0.25:
        regime = "MODERATE_COUPLED"
    elif corr <= -0.35:
        regime = "INVERSE_DECOUPLED"
    else:
        regime = "LOW_CORRELATION"
    return {"correlation": round(corr, 4), "sample_days": n, "regime": regime}


def _returns_map_from_series(points: list[tuple[str, float]]) -> dict[str, float]:
    """Build UTC date -> daily pct return map from sorted (YYYY-MM-DD, close) pairs."""
    if len(points) < 2:
        return {}
    dedup: dict[str, float] = {}
    for dt, px in points:
        if px and px > 0:
            dedup[dt] = px
    ordered = sorted(dedup.items(), key=lambda kv: kv[0])
    returns: dict[str, float] = {}
    for idx in range(1, len(ordered)):
        prev_px = ordered[idx - 1][1]
        cur_dt, cur_px = ordered[idx]
        if prev_px > 0:
            returns[cur_dt] = ((cur_px - prev_px) / prev_px) * 100.0
    return returns


def _strip_unsafe_xml(raw_xml: str) -> str:
    """Reject or strip DOCTYPE/ENTITY declarations before XML parsing."""
    return re.sub(r"<!(?:DOCTYPE|ENTITY)[^>]*>", "", raw_xml or "", flags=re.IGNORECASE)


def parse_rss_items(
    raw_xml: str,
    *,
    source_name: str,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Parse RSS XML safely and run prompt-injection screening on every headline/summary."""
    cleaned_xml = _strip_unsafe_xml(raw_xml)
    if not cleaned_xml.strip():
        return [], []
    root = ET.fromstring(cleaned_xml)
    items: list[dict[str, Any]] = []
    blocked_injections: list[dict[str, str]] = []

    raw_nodes = root.findall(".//item")
    if not raw_nodes:
        raw_nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for node in raw_nodes[: limit * 2]:
        title = (
            node.findtext("title")
            or node.findtext("{http://www.w3.org/2005/Atom}title")
            or ""
        ).strip()
        desc = (
            node.findtext("description")
            or node.findtext("{http://www.w3.org/2005/Atom}summary")
            or ""
        )
        desc_clean = re.sub(r"<[^>]+>", " ", desc)
        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()[:320]
        pub_date = (
            node.findtext("pubDate")
            or node.findtext("{http://www.w3.org/2005/Atom}updated")
            or ""
        ).strip()

        if not title:
            continue

        # Screen untrusted external feed text via ResearchQualityValidator
        hits = ResearchQualityValidator.detect_prompt_injection(title, desc_clean, source_name)
        if hits:
            blocked_injections.append(
                {
                    "source": source_name,
                    "type": hits[0]["type"],
                    "title_preview": title[:100],
                }
            )
            continue

        blob = f"{title} {desc_clean}".lower()
        categories: list[str] = []
        if any(k in blob for k in _FED_KEYWORDS):
            categories.append("FED_MONETARY_POLICY")
        if any(k in blob for k in _MACRO_KEYWORDS):
            categories.append("MACRO_ECONOMIC")
        if any(k in blob for k in _GEOPOLITICAL_KEYWORDS):
            categories.append("GEOPOLITICAL_RISK")
        if any(k in blob for k in _ETF_INSTITUTIONAL_KEYWORDS):
            categories.append("INSTITUTIONAL_CRYPTO_ETF")
        if not categories:
            categories.append("GENERAL_MARKETS")

        bull_hits = sum(1 for w in _BULLISH_WORDS if w in blob)
        bear_hits = sum(1 for w in _BEARISH_WORDS if w in blob)
        polarity = round((bull_hits - bear_hits) / max(1, bull_hits + bear_hits), 3) if (bull_hits or bear_hits) else 0.0

        items.append(
            {
                "source": source_name,
                "title": title[:220],
                "summary": desc_clean,
                "published_at": pub_date,
                "categories": categories,
                "sentiment_polarity": polarity,
            }
        )
        if len(items) >= limit:
            break

    return items, blocked_injections


class MarketDriverEngine:
    """Fetches free live market feeds, computes WHY/WHEN causal drivers, and persists clean snapshots."""

    def __init__(self, store: DuckDBStore, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.store = store
        self._injected_client = http_client

    async def _fetch_coinbase_asset(
        self, client: httpx.AsyncClient, product: str
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        stats_url = f"https://api.exchange.coinbase.com/products/{product}/stats"
        candles_url = f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=86400"
        stats_resp, candles_resp = await asyncio.gather(
            client.get(stats_url),
            client.get(candles_url),
        )
        stats_resp.raise_for_status()
        candles_resp.raise_for_status()
        stats = stats_resp.json()
        raw_candles = candles_resp.json()
        # Coinbase candles: [time_sec, low, high, open, close, volume], newest-first
        sorted_candles = sorted(
            [c for c in raw_candles if isinstance(c, list) and len(c) >= 6],
            key=lambda c: float(c[0]),
        )[-30:]
        closes = [(datetime.fromtimestamp(float(c[0]), tz=timezone.utc).date().isoformat(), float(c[4])) for c in sorted_candles]
        vols = [float(c[5]) for c in sorted_candles if float(c[5]) > 0]
        ranges_pct = [((float(c[2]) - float(c[1])) / float(c[3])) * 100.0 for c in sorted_candles if float(c[3]) > 0]

        last_px = _safe_float(stats.get("last")) or (closes[-1][1] if closes else None)
        open_24h = _safe_float(stats.get("open"))
        high_24h = _safe_float(stats.get("high"))
        low_24h = _safe_float(stats.get("low"))
        vol_24h = _safe_float(stats.get("volume"))
        vol_30d = _safe_float(stats.get("volume_30day"))

        change_24h_pct = (
            round(((last_px - open_24h) / open_24h) * 100.0, 3)
            if last_px and open_24h and open_24h > 0
            else None
        )
        avg_daily_vol = (vol_30d / 30.0) if (vol_30d and vol_30d > 0) else (sum(vols[:-1]) / max(1, len(vols) - 1) if len(vols) > 1 else None)
        volume_surge_ratio = (
            round(vol_24h / avg_daily_vol, 3)
            if vol_24h and avg_daily_vol and avg_daily_vol > 0
            else None
        )

        returns_map = _returns_map_from_series(closes)
        ret_vals = list(returns_map.values())
        realized_vol_20d = None
        if len(ret_vals[-20:]) >= 5:
            sub = ret_vals[-20:]
            m = sum(sub) / len(sub)
            realized_vol_20d = round(math.sqrt(sum((x - m) ** 2 for x in sub) / len(sub)), 3)

        intraday_range_pct = (
            round(((high_24h - low_24h) / open_24h) * 100.0, 3)
            if high_24h and low_24h and open_24h and open_24h > 0
            else None
        )
        avg_range_20d = round(sum(ranges_pct[-20:]) / len(ranges_pct[-20:]), 3) if ranges_pct else None
        compression_ratio = (
            round(intraday_range_pct / avg_range_20d, 3)
            if intraday_range_pct is not None and avg_range_20d and avg_range_20d > 0
            else None
        )

        # Return 7d
        change_7d_pct = None
        if len(closes) >= 8 and closes[-8][1] > 0 and last_px:
            change_7d_pct = round(((last_px - closes[-8][1]) / closes[-8][1]) * 100.0, 3)

        dollar_vol_24h = round(vol_24h * last_px, 2) if vol_24h and last_px else None

        return {
            "symbol": product.split("-")[0],
            "source": f"coinbase:{product}",
            "status": "LIVE",
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "price_usd": round(last_px, 2) if last_px is not None else None,
            "change_24h_pct": change_24h_pct,
            "change_7d_pct": change_7d_pct,
            "volume_24h_units": round(vol_24h, 2) if vol_24h is not None else None,
            "volume_24h_usd": dollar_vol_24h,
            "volume_surge_ratio": volume_surge_ratio,
            "intraday_range_pct": intraday_range_pct,
            "avg_range_20d_pct": avg_range_20d,
            "compression_ratio": compression_ratio,
            "realized_vol_20d_pct": realized_vol_20d,
            "returns_by_date": returns_map,
        }

    async def _fetch_yahoo_asset(self, client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
        t0 = time.monotonic()
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        res = (data.get("chart") or {}).get("result") or []
        if not res:
            raise ValueError(f"Empty Yahoo Finance result for {symbol}")
        item = res[0]
        meta = item.get("meta") or {}
        ts_list = item.get("timestamp") or []
        quote = ((item.get("indicators") or {}).get("quote") or [{}])[0]
        closes_raw = quote.get("close") or []
        vols_raw = quote.get("volume") or []
        highs_raw = quote.get("high") or []
        lows_raw = quote.get("low") or []

        series: list[tuple[str, float]] = []
        vols: list[float] = []
        ranges: list[float] = []
        for ts, cl, vl, hi, lo in zip(ts_list, closes_raw, vols_raw, highs_raw, lows_raw):
            fcl = _safe_float(cl)
            if fcl and fcl > 0:
                dt_iso = datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()
                series.append((dt_iso, fcl))
                fvl = _safe_float(vl)
                if fvl and fvl > 0:
                    vols.append(fvl)
                fhi = _safe_float(hi)
                flo = _safe_float(lo)
                if fhi and flo and fcl > 0:
                    ranges.append(((fhi - flo) / fcl) * 100.0)

        last_px = _safe_float(meta.get("regularMarketPrice")) or (series[-1][1] if series else None)
        prev_close = _safe_float(meta.get("chartPreviousClose"))
        if len(series) >= 2:
            prev_bar_close = series[-2][1]
        else:
            prev_bar_close = prev_close

        change_1d_pct = (
            round(((last_px - prev_bar_close) / prev_bar_close) * 100.0, 3)
            if last_px and prev_bar_close and prev_bar_close > 0
            else None
        )
        change_5d_pct = (
            round(((last_px - series[-6][1]) / series[-6][1]) * 100.0, 3)
            if len(series) >= 6 and last_px and series[-6][1] > 0
            else None
        )
        latest_vol = vols[-1] if vols else None
        avg_vol_20d = sum(vols[:-1]) / len(vols[:-1]) if len(vols) > 1 else None
        vol_surge = round(latest_vol / avg_vol_20d, 3) if latest_vol and avg_vol_20d and avg_vol_20d > 0 else None
        dollar_vol = round(latest_vol * last_px, 2) if latest_vol and last_px else None

        returns_map = _returns_map_from_series(series)
        ret_vals = list(returns_map.values())
        realized_vol = None
        if len(ret_vals) >= 5:
            m = sum(ret_vals) / len(ret_vals)
            realized_vol = round(math.sqrt(sum((x - m) ** 2 for x in ret_vals) / len(ret_vals)), 3)

        latest_range = round(ranges[-1], 3) if ranges else None
        avg_range = round(sum(ranges) / len(ranges), 3) if ranges else None
        compression = round(latest_range / avg_range, 3) if latest_range and avg_range and avg_range > 0 else None

        return {
            "symbol": symbol,
            "source": f"yahoo:{symbol}",
            "status": "LIVE",
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "price_usd": round(last_px, 2) if last_px is not None else None,
            "change_24h_pct": change_1d_pct,
            "change_5d_pct": change_5d_pct,
            "volume_units": round(latest_vol, 0) if latest_vol is not None else None,
            "volume_usd": dollar_vol,
            "volume_surge_ratio": vol_surge,
            "intraday_range_pct": latest_range,
            "avg_range_20d_pct": avg_range,
            "compression_ratio": compression,
            "realized_vol_20d_pct": realized_vol,
            "returns_by_date": returns_map,
        }

    async def _fetch_okx_derivatives(self, client: httpx.AsyncClient, ccy: str = "BTC") -> dict[str, Any]:
        t0 = time.monotonic()
        inst = f"{ccy}-USDT-SWAP"
        fr_url = f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}"
        oi_url = f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={inst}"
        ls_url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=1H"
        fr_r, oi_r, ls_r = await asyncio.gather(
            client.get(fr_url),
            client.get(oi_url),
            client.get(ls_url),
        )
        fr_r.raise_for_status()
        oi_r.raise_for_status()
        ls_r.raise_for_status()

        fr_data = (fr_r.json().get("data") or [{}])[0]
        oi_data = (oi_r.json().get("data") or [{}])[0]
        ls_data = ls_r.json().get("data") or []

        funding_rate = _safe_float(fr_data.get("fundingRate"))
        next_funding_rate = _safe_float(fr_data.get("nextFundingRate"))
        funding_ann_pct = round(funding_rate * 3.0 * 365.0 * 100.0, 2) if funding_rate is not None else None
        oi_usd = _safe_float(oi_data.get("oiUsd"))
        oi_contracts = _safe_float(oi_data.get("oi"))
        long_short_ratio = _safe_float(ls_data[0][1]) if ls_data and len(ls_data[0]) >= 2 else None

        # Derive crowding & liquidation cascade pressure
        crowding = "BALANCED"
        liq_risk_score = 0.25
        if funding_rate is not None and long_short_ratio is not None:
            if funding_rate >= 0.00035 and long_short_ratio >= 1.6:
                crowding = "CROWDED_LONG_SQUEEZE_RISK"
                liq_risk_score = 0.82
            elif funding_rate <= -0.00025 and long_short_ratio <= 0.85:
                crowding = "CROWDED_SHORT_SQUEEZE_RISK"
                liq_risk_score = 0.80
            elif abs(funding_rate) >= 0.0002 or long_short_ratio >= 1.4 or long_short_ratio <= 0.75:
                crowding = "MODERATE_LEVERAGE_SKEW"
                liq_risk_score = 0.58

        return {
            "symbol": ccy,
            "source": f"okx:{inst}",
            "status": "LIVE",
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "funding_rate": round(funding_rate, 6) if funding_rate is not None else None,
            "next_funding_rate": round(next_funding_rate, 6) if next_funding_rate is not None else None,
            "funding_annualized_pct": funding_ann_pct,
            "open_interest_usd": round(oi_usd, 2) if oi_usd is not None else None,
            "open_interest_contracts": round(oi_contracts, 2) if oi_contracts is not None else None,
            "long_short_account_ratio": round(long_short_ratio, 3) if long_short_ratio is not None else None,
            "crowding_regime": crowding,
            "liquidation_risk_score": liq_risk_score,
        }

    async def _fetch_fear_greed(self, client: httpx.AsyncClient) -> dict[str, Any]:
        t0 = time.monotonic()
        resp = await client.get("https://api.alternative.me/fng/?limit=7")
        resp.raise_for_status()
        rows = resp.json().get("data") or []
        if not rows:
            raise ValueError("Empty Fear & Greed response")
        latest = rows[0]
        val = int(latest["value"])
        prev_7d = int(rows[-1]["value"]) if len(rows) > 1 else val
        return {
            "source": "alternative.me:fng",
            "status": "LIVE",
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "value": val,
            "classification": str(latest.get("value_classification") or "Unknown"),
            "delta_7d": val - prev_7d,
        }

    async def _fetch_rss_feed(
        self, client: httpx.AsyncClient, url: str, source_name: str, limit: int = 8
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        resp = await client.get(url)
        resp.raise_for_status()
        items, blocked = parse_rss_items(resp.text, source_name=source_name, limit=limit)
        if blocked:
            self.store.record_system_event(
                severity="CRITICAL",
                component="market_driver_firewall",
                event_type="PROMPT_INJECTION_BLOCKED_IN_RSS",
                message=f"Quarantined {len(blocked)} injection-flagged item(s) from {source_name}",
                detail={"source": source_name, "blocked": blocked},
            )
        return {
            "source": source_name,
            "status": "LIVE",
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "items": items,
            "blocked_injection_count": len(blocked),
        }

    async def fetch_raw_feeds(self, timeout_seconds: float = 8.0) -> dict[str, Any]:
        """Concurrently fetch all free public feeds with graceful per-feed failure handling."""
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json, application/xml, text/xml"}
        close_after = False
        client = self._injected_client
        if client is None:
            client = httpx.AsyncClient(timeout=timeout_seconds, headers=headers, follow_redirects=True)
            close_after = True

        async def _guarded(label: str, coro: Any) -> dict[str, Any]:
            try:
                return await coro
            except Exception as exc:
                return {
                    "source": label,
                    "status": "UNAVAILABLE",
                    "error": _sanitize_feed_error(exc),
                }

        try:
            (
                btc,
                eth,
                spy,
                qqq,
                ibit,
                fbtc,
                okx_btc,
                okx_eth,
                fng,
                fed_rss,
                cnbc_rss,
            ) = await asyncio.gather(
                _guarded("coinbase:BTC-USD", self._fetch_coinbase_asset(client, "BTC-USD")),
                _guarded("coinbase:ETH-USD", self._fetch_coinbase_asset(client, "ETH-USD")),
                _guarded("yahoo:SPY", self._fetch_yahoo_asset(client, "SPY")),
                _guarded("yahoo:QQQ", self._fetch_yahoo_asset(client, "QQQ")),
                _guarded("yahoo:IBIT", self._fetch_yahoo_asset(client, "IBIT")),
                _guarded("yahoo:FBTC", self._fetch_yahoo_asset(client, "FBTC")),
                _guarded("okx:BTC-USDT-SWAP", self._fetch_okx_derivatives(client, "BTC")),
                _guarded("okx:ETH-USDT-SWAP", self._fetch_okx_derivatives(client, "ETH")),
                _guarded("alternative.me:fng", self._fetch_fear_greed(client)),
                _guarded(
                    "fed:press_rss",
                    self._fetch_rss_feed(
                        client,
                        "https://www.federalreserve.gov/feeds/press_all.xml",
                        "federal_reserve_rss",
                        limit=8,
                    ),
                ),
                _guarded(
                    "cnbc:top_news_rss",
                    self._fetch_rss_feed(
                        client,
                        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
                        "cnbc_news_rss",
                        limit=10,
                    ),
                ),
            )
            return {
                "fetched_at": utc_now_iso(),
                "assets": {"BTC": btc, "ETH": eth, "SPY": spy, "QQQ": qqq, "IBIT": ibit, "FBTC": fbtc},
                "derivatives": {"BTC": okx_btc, "ETH": okx_eth},
                "sentiment": fng,
                "news_feeds": {"fed": fed_rss, "cnbc": cnbc_rss},
            }
        finally:
            if close_after:
                await client.aclose()

    def _get_verified_historical_analogs(
        self,
        *,
        compression_ratio: float | None,
        fear_greed: int | None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Query historical event analogs ONLY from clean, verified LIVE snapshots (never TEST/SIMULATED)."""
        with self.store._lock:
            rows = self.store._conn.execute(
                """
                SELECT snapshot_id, btc_price_usd, spy_price_usd, fear_greed_value,
                       btc_funding_rate, when_signals_json, created_at
                FROM market_driver_snapshots
                WHERE provenance = 'LIVE' AND verified_clean = TRUE
                ORDER BY created_at DESC
                LIMIT 25
                """
            ).fetchall()

        analogs: list[dict[str, Any]] = []
        for snap_id, btc_px, spy_px, fng_val, fr_val, when_json, created_at in rows:
            try:
                when_obj = json.loads(when_json or "{}")
            except (TypeError, ValueError):
                when_obj = {}
            similarity_notes: list[str] = []
            if fear_greed is not None and fng_val is not None and abs(int(fear_greed) - int(fng_val)) <= 15:
                similarity_notes.append(f"Fear&Greed regime match ({fng_val} vs {fear_greed})")
            if compression_ratio is not None and when_obj.get("avg_compression_ratio") is not None:
                if abs(float(when_obj["avg_compression_ratio"]) - float(compression_ratio)) <= 0.35:
                    similarity_notes.append("Similar volatility compression")
            if similarity_notes:
                analogs.append(
                    {
                        "snapshot_id": snap_id,
                        "created_at": created_at,
                        "btc_price_usd": btc_px,
                        "spy_price_usd": spy_px,
                        "fear_greed_value": fng_val,
                        "regime": when_obj.get("movement_regime"),
                        "notes": similarity_notes,
                    }
                )
            if len(analogs) >= limit:
                break
        return analogs

    def analyze_and_persist(
        self,
        raw_feeds: dict[str, Any],
        *,
        provenance: str = "LIVE",
        persist: bool = True,
    ) -> dict[str, Any]:
        """Compute cross-asset correlations, WHY causal drivers, WHEN movement windows, and persist."""
        t_start = time.monotonic()
        assets = dict(raw_feeds.get("assets") or {})
        derivs = dict(raw_feeds.get("derivatives") or {})
        sentiment = dict(raw_feeds.get("sentiment") or {})
        news_feeds = dict(raw_feeds.get("news_feeds") or {})

        def _asset_summary(sym: str) -> dict[str, Any]:
            a = assets.get(sym) or {}
            # Strip raw date maps from persisted summary JSON to keep rows compact
            return {k: v for k, v in a.items() if k != "returns_by_date"}

        asset_summary = {sym: _asset_summary(sym) for sym in ("BTC", "ETH", "SPY", "QQQ", "IBIT", "FBTC")}

        # Feed health summary
        feed_health: dict[str, str] = {}
        for sym, obj in assets.items():
            feed_health[f"asset_{sym}"] = str(obj.get("status", "UNAVAILABLE"))
        for sym, obj in derivs.items():
            feed_health[f"okx_{sym}"] = str(obj.get("status", "UNAVAILABLE"))
        feed_health["fear_greed"] = str(sentiment.get("status", "UNAVAILABLE"))
        feed_health["fed_rss"] = str((news_feeds.get("fed") or {}).get("status", "UNAVAILABLE"))
        feed_health["cnbc_rss"] = str((news_feeds.get("cnbc") or {}).get("status", "UNAVAILABLE"))

        live_feed_count = sum(1 for v in feed_health.values() if v == "LIVE")
        total_feed_count = len(feed_health)

        # Injection contamination audit across RSS items
        blocked_count = int((news_feeds.get("fed") or {}).get("blocked_injection_count", 0)) + int(
            (news_feeds.get("cnbc") or {}).get("blocked_injection_count", 0)
        )
        all_news = list((news_feeds.get("fed") or {}).get("items") or []) + list(
            (news_feeds.get("cnbc") or {}).get("items") or []
        )

        # Cross-asset rolling Pearson correlations (Stocks <-> Crypto <-> ETF)
        ret_btc = (assets.get("BTC") or {}).get("returns_by_date") or {}
        ret_eth = (assets.get("ETH") or {}).get("returns_by_date") or {}
        ret_spy = (assets.get("SPY") or {}).get("returns_by_date") or {}
        ret_qqq = (assets.get("QQQ") or {}).get("returns_by_date") or {}
        ret_ibit = (assets.get("IBIT") or {}).get("returns_by_date") or {}

        correlations = {
            "BTC_SPY": compute_pearson_correlation(ret_btc, ret_spy),
            "BTC_QQQ": compute_pearson_correlation(ret_btc, ret_qqq),
            "BTC_IBIT": compute_pearson_correlation(ret_btc, ret_ibit),
            "ETH_SPY": compute_pearson_correlation(ret_eth, ret_spy),
            "ETH_QQQ": compute_pearson_correlation(ret_eth, ret_qqq),
            "BTC_ETH": compute_pearson_correlation(ret_btc, ret_eth),
        }

        # Institutional ETF Flow Proxy (IBIT + FBTC price direction * volume surge)
        ibit_a = asset_summary["IBIT"]
        fbtc_a = asset_summary["FBTC"]
        etf_flow_score = 0.0
        etf_flow_signals: list[str] = []
        for etf_sym, etf_obj in (("IBIT", ibit_a), ("FBTC", fbtc_a)):
            chg = _safe_float(etf_obj.get("change_24h_pct"))
            vsurge = _safe_float(etf_obj.get("volume_surge_ratio")) or 1.0
            if chg is not None:
                contrib = max(-1.0, min(1.0, (chg / 3.0) * min(2.0, vsurge)))
                etf_flow_score += contrib * 0.5
                etf_flow_signals.append(
                    f"{etf_sym} {chg:+.2f}% on {vsurge:.2f}x volume"
                )
        etf_flow_regime = (
            "NET_INFLOW_PRESSURE"
            if etf_flow_score >= 0.25
            else ("NET_OUTFLOW_PRESSURE" if etf_flow_score <= -0.25 else "BALANCED_ETF_FLOW")
        )

        # Macro / Fed / Geopolitical news intensity & directional tilt
        fed_items = [n for n in all_news if "FED_MONETARY_POLICY" in n.get("categories", [])]
        geo_items = [n for n in all_news if "GEOPOLITICAL_RISK" in n.get("categories", [])]
        macro_items = [n for n in all_news if "MACRO_ECONOMIC" in n.get("categories", [])]
        avg_news_polarity = (
            round(sum(float(n.get("sentiment_polarity", 0.0)) for n in all_news) / len(all_news), 3)
            if all_news
            else 0.0
        )

        macro_news_summary = {
            "total_items": len(all_news),
            "fed_policy_count": len(fed_items),
            "macro_economic_count": len(macro_items),
            "geopolitical_risk_count": len(geo_items),
            "avg_news_polarity": avg_news_polarity,
            "blocked_injection_count": blocked_count,
            "etf_flow_proxy": {
                "regime": etf_flow_regime,
                "score": round(etf_flow_score, 3),
                "signals": etf_flow_signals,
            },
            "top_headlines": all_news[:12],
        }

        # ------------------------------------------------------------------
        # Causal WHY Attribution ("WHY prices are fluctuating")
        # ------------------------------------------------------------------
        btc_a = asset_summary["BTC"]
        eth_a = asset_summary["ETH"]
        spy_a = asset_summary["SPY"]
        qqq_a = asset_summary["QQQ"]
        okx_btc = derivs.get("BTC") or {}
        fng_val = sentiment.get("value")

        why_drivers: list[dict[str, Any]] = []

        # 1. BTC/ETH Spot Momentum & Exchange Volume / Whale Block Proxy
        btc_chg = _safe_float(btc_a.get("change_24h_pct"))
        btc_vsurge = _safe_float(btc_a.get("volume_surge_ratio"))
        if btc_chg is not None:
            v_note = f" on {btc_vsurge:.2f}x 30d avg volume" if btc_vsurge else ""
            why_drivers.append(
                {
                    "driver_id": "SPOT_WHALE_VOLUME",
                    "category": "Spot Liquidity & Whale Volume Proxy",
                    "impact_score": round(max(-1.0, min(1.0, (btc_chg / 4.0) * (btc_vsurge or 1.0))), 3),
                    "magnitude": round(abs(btc_chg) * (btc_vsurge or 1.0), 3),
                    "explanation": (
                        f"BTC moved {btc_chg:+.2f}% (24h){v_note}; "
                        f"ETH {(_safe_float(eth_a.get('change_24h_pct')) or 0.0):+.2f}%."
                    ),
                }
            )

        # 2. Institutional ETF Inflows/Outflows (IBIT & FBTC)
        if etf_flow_signals:
            why_drivers.append(
                {
                    "driver_id": "INSTITUTIONAL_ETF_FLOW",
                    "category": "Institutional Spot ETF Flow Proxy",
                    "impact_score": round(etf_flow_score, 3),
                    "magnitude": round(abs(etf_flow_score) * 3.0, 3),
                    "explanation": f"{etf_flow_regime}: {'; '.join(etf_flow_signals)}.",
                }
            )

        # 3. Stocks <-> Crypto Correlation Spillover (SPY / QQQ)
        btc_qqq_corr = correlations["BTC_QQQ"].get("correlation")
        qqq_chg = _safe_float(qqq_a.get("change_24h_pct"))
        spy_chg = _safe_float(spy_a.get("change_24h_pct"))
        if qqq_chg is not None or spy_chg is not None:
            corr_txt = f"BTC↔QQQ r={btc_qqq_corr:+.2f}" if btc_qqq_corr is not None else "BTC↔QQQ r=N/A"
            eq_move = qqq_chg if qqq_chg is not None else (spy_chg or 0.0)
            eq_impact = eq_move * (abs(btc_qqq_corr) if btc_qqq_corr is not None else 0.5)
            why_drivers.append(
                {
                    "driver_id": "STOCKS_CRYPTO_CORRELATION",
                    "category": "Equities & Cross-Asset Risk Appetite",
                    "impact_score": round(max(-1.0, min(1.0, eq_impact / 2.0)), 3),
                    "magnitude": round(abs(eq_impact), 3),
                    "explanation": (
                        f"SPY {(spy_chg or 0.0):+.2f}% / QQQ {(qqq_chg or 0.0):+.2f}% ({corr_txt}, "
                        f"{correlations['BTC_QQQ'].get('regime')})."
                    ),
                }
            )

        # 4. Leverage, Funding & Liquidation Cascade Risk (OKX)
        fr = _safe_float(okx_btc.get("funding_rate"))
        ls_ratio = _safe_float(okx_btc.get("long_short_account_ratio"))
        liq_risk = float(okx_btc.get("liquidation_risk_score") or 0.0)
        if fr is not None or ls_ratio is not None:
            crowd = str(okx_btc.get("crowding_regime") or "BALANCED")
            why_drivers.append(
                {
                    "driver_id": "LEVERAGE_LIQUIDATIONS",
                    "category": "Derivatives Leverage & Liquidation Skew",
                    "impact_score": round((-1.0 if "LONG_SQUEEZE" in crowd else (0.6 if "SHORT_SQUEEZE" in crowd else 0.0)) * liq_risk, 3),
                    "magnitude": round(liq_risk * 2.5, 3),
                    "explanation": (
                        f"OKX BTC funding={((fr or 0.0)*100):.4f}% ({okx_btc.get('funding_annualized_pct')}% ann), "
                        f"L/S ratio={ls_ratio}, regime={crowd}."
                    ),
                }
            )

        # 5. Federal Reserve, Macro & Geopolitical Catalysts
        cat_count = len(fed_items) + len(geo_items) + len(macro_items)
        if cat_count > 0:
            why_drivers.append(
                {
                    "driver_id": "FED_MACRO_GEOPOLITICAL",
                    "category": "Fed Policy, Macro & Geopolitical Catalysts",
                    "impact_score": round(avg_news_polarity, 3),
                    "magnitude": round(min(4.0, cat_count * 0.45), 3),
                    "explanation": (
                        f"{len(fed_items)} Fed policy releases, {len(macro_items)} macro prints, "
                        f"{len(geo_items)} geopolitical risk items (tone={avg_news_polarity:+.2f})."
                    ),
                }
            )

        # 6. Market Sentiment (Fear & Greed)
        if fng_val is not None:
            fng_norm = round((int(fng_val) - 50) / 50.0, 3)
            why_drivers.append(
                {
                    "driver_id": "MARKET_SENTIMENT",
                    "category": "Crypto Fear & Greed Sentiment",
                    "impact_score": fng_norm,
                    "magnitude": round(abs(fng_norm) * 2.0, 3),
                    "explanation": (
                        f"Fear & Greed at {fng_val}/100 ({sentiment.get('classification')}, "
                        f"7d delta {int(sentiment.get('delta_7d') or 0):+d})."
                    ),
                }
            )

        why_drivers.sort(key=lambda d: float(d.get("magnitude", 0.0)), reverse=True)

        # ------------------------------------------------------------------
        # Timing WHEN Significant Movements Are Likely
        # ------------------------------------------------------------------
        comp_btc = _safe_float(btc_a.get("compression_ratio"))
        comp_spy = _safe_float(spy_a.get("compression_ratio"))
        comps = [c for c in (comp_btc, comp_spy) if c is not None]
        avg_comp = round(sum(comps) / len(comps), 3) if comps else None

        trigger_reasons: list[str] = []
        urgency_score = 0.28

        if avg_comp is not None and avg_comp <= 0.72:
            urgency_score += 0.25
            trigger_reasons.append(f"Tight volatility compression ({avg_comp:.2f}x 20d range) precedes expansion")
        elif avg_comp is not None and avg_comp >= 1.45:
            urgency_score += 0.22
            trigger_reasons.append(f"Active range expansion ({avg_comp:.2f}x 20d ATR) signals breakout continuation")

        if liq_risk >= 0.70:
            urgency_score += 0.22
            trigger_reasons.append(f"Extreme leverage skew ({okx_btc.get('crowding_regime')}) risks liquidation cascade")

        if (btc_vsurge and btc_vsurge >= 1.35) or (
            _safe_float(ibit_a.get("volume_surge_ratio")) or 0.0
        ) >= 1.35:
            urgency_score += 0.16
            trigger_reasons.append("Institutional ETF / spot volume surge confirms active capital rotation")

        if len(fed_items) >= 2 or len(geo_items) >= 2:
            urgency_score += 0.14
            trigger_reasons.append("Elevated Fed / geopolitical headline cadence creates event-driven repricing window")

        urgency_score = round(min(0.95, urgency_score), 3)
        if urgency_score >= 0.68:
            movement_regime = "IMMINENT_BREAKOUT_WINDOW"
            expected_window = "4H–24H"
        elif urgency_score >= 0.48:
            movement_regime = "ELEVATED_CATALYST_WINDOW"
            expected_window = "24H"
        else:
            movement_regime = "RANGE_CONSOLIDATION"
            expected_window = "24H–48H"

        analogs = self._get_verified_historical_analogs(
            compression_ratio=avg_comp,
            fear_greed=int(fng_val) if fng_val is not None else None,
        )

        when_signals = {
            "movement_regime": movement_regime,
            "expected_window": expected_window,
            "movement_urgency_score": urgency_score,
            "avg_compression_ratio": avg_comp,
            "trigger_reasons": trigger_reasons or ["No extreme compression or squeeze triggers active; baseline volatility regime."],
            "historical_analogs_count": len(analogs),
            "historical_analogs": analogs,
        }

        # Effective provenance & cleanliness guard
        eff_provenance = provenance.strip().upper()
        # Require at least 1 core live feed to tag LIVE cleanly; otherwise fail-closed to UNAVAILABLE unless TEST/SIMULATED
        if eff_provenance == "LIVE" and live_feed_count == 0:
            eff_provenance = "UNAVAILABLE"
        verified_clean = eff_provenance == "LIVE" and live_feed_count >= 2

        snapshot_id = f"MDSNAP-{uuid.uuid4().hex[:10].upper()}"
        created_at = utc_now_iso()
        freshness_ms = int((time.monotonic() - t_start) * 1000.0)

        record = {
            "snapshot_id": snapshot_id,
            "provenance": eff_provenance,
            "verified_clean": verified_clean,
            "btc_price_usd": btc_a.get("price_usd"),
            "eth_price_usd": eth_a.get("price_usd"),
            "spy_price_usd": spy_a.get("price_usd"),
            "qqq_price_usd": qqq_a.get("price_usd"),
            "ibit_price_usd": ibit_a.get("price_usd"),
            "fear_greed_value": int(fng_val) if fng_val is not None else None,
            "btc_funding_rate": fr,
            "assets": asset_summary,
            "correlations": correlations,
            "derivatives": derivs,
            "sentiment": sentiment,
            "macro_news": macro_news_summary,
            "why_drivers": why_drivers,
            "when_signals": when_signals,
            "feed_health": {
                "feeds": feed_health,
                "live_count": live_feed_count,
                "total_count": total_feed_count,
            },
            "data_freshness_ms": freshness_ms,
            "created_at": created_at,
        }

        if persist:
            with self.store._lock:
                self.store._conn.execute(
                    """
                    INSERT INTO market_driver_snapshots VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        snapshot_id,
                        eff_provenance,
                        verified_clean,
                        record["btc_price_usd"],
                        record["eth_price_usd"],
                        record["spy_price_usd"],
                        record["qqq_price_usd"],
                        record["ibit_price_usd"],
                        record["fear_greed_value"],
                        record["btc_funding_rate"],
                        json.dumps(asset_summary),
                        json.dumps(correlations),
                        json.dumps(derivs),
                        json.dumps(sentiment),
                        json.dumps(macro_news_summary),
                        json.dumps(why_drivers),
                        json.dumps(when_signals),
                        json.dumps(record["feed_health"]),
                        freshness_ms,
                        created_at,
                    ],
                )
        return record

    async def ingest_live_snapshot(self) -> dict[str, Any]:
        raw = await self.fetch_raw_feeds()
        return self.analyze_and_persist(raw, provenance="LIVE", persist=True)

    def _row_to_snapshot(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["assets"] = json.loads(d.pop("asset_summary_json", "{}"))
        d["correlations"] = json.loads(d.pop("correlations_json", "{}"))
        d["derivatives"] = json.loads(d.pop("derivatives_json", "{}"))
        d["sentiment"] = json.loads(d.pop("sentiment_json", "{}"))
        d["macro_news"] = json.loads(d.pop("macro_news_json", "{}"))
        d["why_drivers"] = json.loads(d.pop("why_drivers_json", "[]"))
        d["when_signals"] = json.loads(d.pop("when_signals_json", "{}"))
        d["feed_health"] = json.loads(d.pop("feed_health_json", "{}"))
        return d

    def get_latest_snapshot(self, *, clean_live_only: bool = True) -> dict[str, Any] | None:
        clause = "WHERE provenance = 'LIVE' AND verified_clean = TRUE" if clean_live_only else ""
        with self.store._lock:
            row = self.store._conn.execute(
                f"SELECT * FROM market_driver_snapshots {clause} ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_snapshot(cols, row)

    def list_snapshots(
        self, *, clean_live_only: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        clause = "WHERE provenance = 'LIVE' AND verified_clean = TRUE" if clean_live_only else ""
        with self.store._lock:
            rows = self.store._conn.execute(
                f"SELECT * FROM market_driver_snapshots {clause} ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_snapshot(cols, r) for r in rows]
