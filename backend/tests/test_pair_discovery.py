"""Deterministic tests for Polymarket <-> Kalshi pair discovery (no network)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx

from backend.arbitrage.pair_discovery import (
    discover_pairs,
    match_markets,
    register_candidates,
    score_candidate,
)
from backend.storage.duckdb_store import DuckDBStore
from backend.venues.kalshi import KalshiBinaryMarket
from backend.venues.polymarket import PolymarketBinaryMarket


def _poly(mid: str, question: str, end: str = "2028-11-08T04:59:00Z", **kw) -> PolymarketBinaryMarket:
    base = dict(
        market_id=mid, condition_id=f"0x{mid}", question=question, slug=mid, end_date=end,
        yes_token_id=f"Y{mid}", no_token_id=f"N{mid}", fees_enabled=True, fee_type="politics_fees",
        fee_rate=0.04, fee_exponent=1.0, neg_risk=True, neg_risk_other=False, volume_24hr=1000.0,
        liquidity_usd=5000.0, identical_settlement_verified=True,
    )
    base.update(kw)
    return PolymarketBinaryMarket(**base)


def _kalshi(ticker: str, title: str, sub: str, exp: str = "2028-11-07T15:00:00Z") -> KalshiBinaryMarket:
    return KalshiBinaryMarket(
        ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], series_ticker="", title=title,
        subtitle="", yes_sub_title=sub, no_sub_title="", rules_primary="", rules_secondary="",
        open_time="", close_time=exp, expiration_time=exp, updated_time="", updated_at_ms=0,
        can_close_early=True, yes_bid_dollars=None, yes_ask_dollars=None, no_bid_dollars=None,
        no_ask_dollars=None, volume_24h=10.0, open_interest=10.0,
    )


NEWSOM_P = _poly("559652", "Will Gavin Newsom win the 2028 Democratic presidential nomination?")
NEWSOM_K = _kalshi("KXPRESNOMD-28-GN", "Will Gavin Newsom be the Democratic Presidential nominee in 2028?", "Gavin Newsom")
NEWSOM_VP_K = _kalshi("KXVPRESNOMD-28-GN", "Will Gavin Newsom be the nominee for the Vice Presidency for the Democratic party?", "Gavin Newsom")
SHAPIRO_K = _kalshi("KXPRESNOMD-28-JS", "Will Josh Shapiro be the Democratic Presidential nominee in 2028?", "Josh Shapiro")
DIMON_RUN_K = _kalshi("KXPRESRUN-28-JDIM", "Will Jamie Dimon announce a run for President of the United States in 2028?", "Jamie Dimon")
EVENT = {"KXPRESNOMD-28-GN": "2028 Democratic presidential nominee", "KXVPRESNOMD-28-GN": "2028 Democratic VP nominee",
         "KXPRESNOMD-28-JS": "2028 Democratic presidential nominee"}


def test_true_cross_venue_match_scores_high():
    cand = score_candidate(NEWSOM_P, NEWSOM_K, kalshi_event_title=EVENT["KXPRESNOMD-28-GN"])
    assert cand is not None
    assert cand.similarity >= 0.8
    assert cand.cutoff_delta_hours == 13.98
    assert {"gavin", "newsom"} <= set(cand.shared_tokens)


def test_hard_predicate_mismatches_are_rejected():
    # Vice President vs President nomination
    assert score_candidate(NEWSOM_P, NEWSOM_VP_K, kalshi_event_title=EVENT["KXVPRESNOMD-28-GN"]) is None
    # Different person sharing a first name (Josh Stein vs Josh Shapiro)
    stein = _poly("1", "Will Josh Stein win the 2028 Democratic presidential nomination?")
    assert score_candidate(stein, SHAPIRO_K, common_tokens=frozenset({"2028", "democrat", "presid", "nomin"})) is None
    # Winning the election vs announcing a run
    dimon = _poly("2", "Will Jamie Dimon win the 2028 US Presidential Election?")
    assert score_candidate(dimon, DIMON_RUN_K) is None
    # Party mismatch
    rep = _poly("3", "Will Gavin Newsom win the 2028 Republican presidential nomination?")
    assert score_candidate(rep, NEWSOM_K) is None
    # Year / number mismatch
    y2032 = _poly("4", "Will Gavin Newsom win the 2032 Democratic presidential nomination?", end="2028-11-08T04:59:00Z")
    assert score_candidate(y2032, NEWSOM_K) is None


def test_cutoff_misalignment_and_catchall_rejected():
    late = replace(NEWSOM_P, end_date="2028-12-31T00:00:00Z")
    assert score_candidate(late, NEWSOM_K) is None
    catchall = replace(NEWSOM_P, neg_risk_other=True)
    assert score_candidate(catchall, NEWSOM_K) is None


def test_matching_is_one_to_one_and_prefers_best():
    matches = match_markets(
        [NEWSOM_P], [NEWSOM_VP_K, NEWSOM_K, SHAPIRO_K], kalshi_event_titles=EVENT, common_token_df=2
    )
    assert [(c.poly_market_id, c.kalshi_ticker) for c, _, _ in matches] == [("559652", "KXPRESNOMD-28-GN")]


def test_registration_is_unverified_idempotent_and_never_overwrites_attestations():
    store = DuckDBStore(db_path=":memory:")
    try:
        matches = match_markets([NEWSOM_P], [NEWSOM_K], kalshi_event_titles=EVENT)
        preview = register_candidates(store, matches, register=False)
        assert preview[0].registered is False and store.list_cross_venue_pairs() == []

        res = register_candidates(store, matches, register=True)
        assert res[0].registered is True
        pair = store.get_cross_venue_pair(res[0].pair_id)
        assert pair is not None
        assert pair["identical_settlement_verified"] is False
        assert pair["predicate_verified"] is False and pair["resolution_source_verified"] is False
        assert pair["settlement_status"] in ("UNVERIFIED_FAIL_CLOSED", "MISMATCH_BLOCKED")
        assert pair["notes"].startswith("AUTO_DISCOVERED")

        # Simulate a human attestation, then re-run discovery: it must be left untouched.
        store.upsert_cross_venue_pair({**pair, "predicate_verified": True, "notes": "HUMAN"})
        again = register_candidates(store, matches, register=True)
        assert again[0].registered is False
        assert again[0].skip_reason.startswith("Already registered")
        kept = store.get_cross_venue_pair(res[0].pair_id)
        assert kept["predicate_verified"] is True and kept["notes"] == "HUMAN"
        assert len(store.list_cross_venue_pairs()) == 1
    finally:
        store.close()


def test_discover_pairs_end_to_end_with_mock_http_and_429_backoff(monkeypatch):
    import backend.arbitrage.pair_discovery as pd

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(pd.asyncio, "sleep", _no_sleep)
    calls = {"kalshi": 0}

    gamma_row = {
        "id": "559652", "conditionId": "0xabc", "question": NEWSOM_P.question, "slug": "newsom",
        "endDate": "2028-11-08T04:59:00Z", "outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]',
        "enableOrderBook": True, "acceptingOrders": True, "negRisk": True, "negRiskOther": False,
        "feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1},
    }
    kalshi_market = {
        "ticker": "KXPRESNOMD-28-GN", "event_ticker": "KXPRESNOMD-28", "market_type": "binary",
        "status": "active", "title": NEWSOM_K.title, "yes_sub_title": "Gavin Newsom",
        "close_time": "2028-11-07T15:00:00Z", "expiration_time": "2028-11-07T15:00:00Z",
        "can_close_early": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "gamma-api" in request.url.host:
            offset = int(request.url.params.get("offset", "0"))
            return httpx.Response(200, json=[gamma_row] if offset == 0 else [])
        calls["kalshi"] += 1
        if calls["kalshi"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"events": [{
            "series_ticker": "KXPRESNOMD", "title": "2028 Democratic presidential nominee",
            "markets": [kalshi_market],
        }], "cursor": ""})

    store = DuckDBStore(db_path=":memory:")
    try:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        out = asyncio.run(discover_pairs(store, register=True, http_client=client))
        asyncio.run(client.aclose())
        assert calls["kalshi"] == 2  # retried after 429
        assert out["candidates_found"] == 1 and out["registered_count"] == 1
        assert out["verified_count"] == 0
        assert store.list_cross_venue_pairs()[0]["kalshi_ticker"] == "KXPRESNOMD-28-GN"
    finally:
        store.close()


def test_discover_endpoint_requires_auth_and_defaults_to_dry_run(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import backend.main as main_mod
    from backend.tests.auth_helpers import authenticate_test_client

    seen: dict = {}

    async def _fake_discover(store, **kwargs):
        seen.update(kwargs)
        return {"provenance": "LIVE", "mode": "DRY_RUN", "candidates": [], "verified_count": 0}

    monkeypatch.setattr(main_mod, "discover_pairs", _fake_discover)
    app = main_mod.create_app(db_path=str(tmp_path / "disc.duckdb"))
    with TestClient(app) as client:
        assert client.post("/api/cross-venue/discover", json={}).status_code in (401, 403)
        authenticate_test_client(client)
        resp = client.post("/api/cross-venue/discover", json={})
        assert resp.status_code == 200
        assert resp.json()["verified_count"] == 0
        assert seen["register"] is False  # dry-run by default
        client.post("/api/cross-venue/discover", json={"persist": True})
        assert seen["register"] is True
