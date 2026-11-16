"""Human review support for cross-venue pairs.

Builds a side-by-side settlement-rules comparison for a registered pair from
live, read-only public venue metadata, and defines the review lifecycle:

- PENDING_REVIEW: registered (usually auto-discovered) and not yet attested.
- CONFIRMED: an operator attested all settlement checks and the matcher
  verified the pair (identical_settlement_verified=True).
- REJECTED: an operator rejected the pair. Rejected pairs keep every
  attestation False, are skipped by the cross-venue scanner, and are never
  re-added by discovery (discovery skips any existing poly/kalshi pair).

Nothing here can verify a pair by itself; confirmation still flows through
POST /api/cross-venue/pairs and the fail-closed matcher.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx

from backend.arbitrage.matcher import assess_contract_equivalence
from backend.venues.kalshi import DEFAULT_HEADERS as KALSHI_HEADERS
from backend.venues.kalshi import KALSHI_BASE_URL, parse_kalshi_market
from backend.venues.polymarket import DEFAULT_HEADERS as POLY_HEADERS
from backend.venues.polymarket import GAMMA_BASE_URL, parse_gamma_binary_market

REJECTED_NOTE_PREFIX = "REJECTED_BY_REVIEW"
CONFIRMED_NOTE_PREFIX = "HUMAN_REVIEWED"

ReviewState = Literal["PENDING_REVIEW", "CONFIRMED", "REJECTED"]


def pair_review_state(pair: dict[str, Any]) -> ReviewState:
    if str(pair.get("notes") or "").startswith(REJECTED_NOTE_PREFIX):
        return "REJECTED"
    if pair.get("identical_settlement_verified"):
        return "CONFIRMED"
    return "PENDING_REVIEW"


def prioritize_pairs_for_scan(pairs: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    """Confirmed pairs first, then pending; rejected pairs are excluded."""
    active = [p for p in pairs if pair_review_state(p) != "REJECTED"]
    confirmed = [p for p in active if p.get("identical_settlement_verified")]
    pending = [p for p in active if not p.get("identical_settlement_verified")]
    confirmed.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    pending.sort(key=lambda p: str(p.get("created_at") or ""), reverse=True)
    return (confirmed + pending)[:limit]


def build_rejected_record(pair: dict[str, Any], reason: str) -> dict[str, Any]:
    """Rejection clears every attestation so the pair can never pass the Risk Gate."""
    clean_reason = " ".join((reason or "").split())[:400]
    return {
        **pair,
        "resolution_source_verified": False,
        "predicate_verified": False,
        "polarity_inverted": False,
        "void_rules_compatible": False,
        "allow_early_close_variance": False,
        "identical_settlement_verified": False,
        "settlement_status": "MISMATCH_BLOCKED",
        "mismatch_reasons": [f"Rejected by human review: {clean_reason}"],
        "notes": f"{REJECTED_NOTE_PREFIX}: {clean_reason}",
    }


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> dict[str, Any] | None:
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def build_pair_review(
    pair: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Fetch both venues' live settlement rules and the current matcher assessment."""
    owns = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s)
    poly_id = str(pair["poly_market_id"])
    ticker = str(pair["kalshi_ticker"])
    try:
        poly_raw, kalshi_payload = await asyncio.gather(
            _get_json(client, f"{GAMMA_BASE_URL}/markets/{poly_id}", POLY_HEADERS),
            _get_json(client, f"{KALSHI_BASE_URL}/markets/{ticker}", KALSHI_HEADERS),
        )
        kalshi_raw = (kalshi_payload or {}).get("market") if kalshi_payload else None
        event_ticker = str((kalshi_raw or {}).get("event_ticker") or "")
        event_payload = (
            await _get_json(client, f"{KALSHI_BASE_URL}/events/{event_ticker}", KALSHI_HEADERS)
            if event_ticker
            else None
        )
        event = (event_payload or {}).get("event") or {}
        series_ticker = str(event.get("series_ticker") or "")
        series_payload = (
            await _get_json(client, f"{KALSHI_BASE_URL}/series/{series_ticker}", KALSHI_HEADERS)
            if series_ticker
            else None
        )
        series = (series_payload or {}).get("series") or {}
    finally:
        if owns:
            await client.aclose()

    errors: list[str] = []
    if poly_raw is None:
        errors.append(f"Polymarket market {poly_id} unavailable")
    if kalshi_raw is None:
        errors.append(f"Kalshi market {ticker} unavailable")

    assessment_out: dict[str, Any] | None = None
    poly_mkt = parse_gamma_binary_market(poly_raw) if poly_raw else None
    kalshi_mkt = parse_kalshi_market(kalshi_raw, series_ticker=series_ticker) if kalshi_raw else None
    if poly_mkt is not None and kalshi_mkt is not None:
        a = assess_contract_equivalence(
            poly_market=poly_mkt,
            kalshi_market=kalshi_mkt,
            resolution_source_verified=bool(pair.get("resolution_source_verified")),
            predicate_verified=bool(pair.get("predicate_verified")),
            polarity_inverted=bool(pair.get("polarity_inverted")),
            void_rules_compatible=bool(pair.get("void_rules_compatible")),
            allow_early_close_variance=bool(pair.get("allow_early_close_variance")),
        )
        assessment_out = {
            "identical_settlement_verified": a.identical_settlement_verified,
            "status": a.status,
            "cutoff_delta_hours": a.cutoff_delta_hours,
            "mismatch_reasons": list(a.mismatch_reasons),
            "checks": list(a.checks),
        }
    elif poly_raw is not None and poly_mkt is None:
        errors.append("Polymarket market is no longer an active binary order-book market")
    elif kalshi_raw is not None and kalshi_mkt is None:
        errors.append("Kalshi market is no longer an active binary market")

    poly_side = None
    if poly_raw is not None:
        slug = str(poly_raw.get("slug") or "")
        poly_side = {
            "market_id": poly_id,
            "question": str(poly_raw.get("question") or pair.get("poly_question") or ""),
            "rules_text": str(poly_raw.get("description") or ""),
            "resolution_source": str(poly_raw.get("resolutionSource") or ""),
            "end_date": str(poly_raw.get("endDate") or ""),
            "neg_risk": bool(poly_raw.get("negRisk", False)),
            "neg_risk_other": bool(poly_raw.get("negRiskOther", False)),
            "url": f"https://polymarket.com/market/{slug}" if slug else None,
        }

    kalshi_side = None
    if kalshi_raw is not None:
        kalshi_side = {
            "ticker": ticker,
            "title": str(kalshi_raw.get("title") or ""),
            "event_title": str(event.get("title") or ""),
            "yes_sub_title": str(kalshi_raw.get("yes_sub_title") or ""),
            "rules_primary": str(kalshi_raw.get("rules_primary") or ""),
            "rules_secondary": str(kalshi_raw.get("rules_secondary") or ""),
            "early_close_condition": str(kalshi_raw.get("early_close_condition") or ""),
            "can_close_early": bool(kalshi_raw.get("can_close_early", False)),
            "close_time": str(kalshi_raw.get("close_time") or ""),
            "expiration_time": str(kalshi_raw.get("expiration_time") or ""),
            "expected_expiration_time": str(kalshi_raw.get("expected_expiration_time") or ""),
            "settlement_timer_seconds": kalshi_raw.get("settlement_timer_seconds"),
            "settlement_sources": [
                {"name": str(s.get("name") or ""), "url": str(s.get("url") or "")}
                for s in (event.get("settlement_sources") or [])
                if isinstance(s, dict)
            ],
            "contract_url": str(series.get("contract_url") or "") or None,
            "url": f"https://kalshi.com/markets/{series_ticker.lower()}" if series_ticker else None,
        }

    return {
        "provenance": "LIVE",
        "pair": pair,
        "review_state": pair_review_state(pair),
        "polymarket": poly_side,
        "kalshi": kalshi_side,
        "assessment": assessment_out,
        "errors": errors,
    }
