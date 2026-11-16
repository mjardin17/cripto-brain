"""Tests for the discovered-pair human review lifecycle (offline, no network)."""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from backend.arbitrage.pair_review import (
    REJECTED_NOTE_PREFIX,
    build_pair_review,
    build_rejected_record,
    pair_review_state,
    prioritize_pairs_for_scan,
)
from backend.main import create_app
from backend.tests.auth_helpers import authenticate_test_client


def _pair(pair_id: str, *, verified: bool = False, notes: str = "", created_at: str = "2026-11-01T00:00:00+00:00"):
    return {
        "pair_id": pair_id,
        "poly_market_id": f"POLY-{pair_id}",
        "poly_question": f"Question {pair_id}?",
        "kalshi_ticker": f"KX-{pair_id}",
        "kalshi_title": f"Title {pair_id}",
        "resolution_source_verified": verified,
        "predicate_verified": verified,
        "polarity_inverted": False,
        "void_rules_compatible": verified,
        "allow_early_close_variance": verified,
        "identical_settlement_verified": verified,
        "settlement_status": "VERIFIED" if verified else "UNVERIFIED_FAIL_CLOSED",
        "mismatch_reasons": [],
        "cutoff_delta_hours": 1.0,
        "notes": notes,
        "created_at": created_at,
    }


def test_review_state_classification():
    assert pair_review_state(_pair("A")) == "PENDING_REVIEW"
    assert pair_review_state(_pair("B", verified=True)) == "CONFIRMED"
    assert pair_review_state(_pair("C", notes=f"{REJECTED_NOTE_PREFIX}: wrong event")) == "REJECTED"


def test_prioritize_confirmed_first_and_excludes_rejected():
    pairs = [
        _pair("P1", created_at="2026-11-03T00:00:00+00:00"),
        _pair("C1", verified=True, created_at="2026-11-01T00:00:00+00:00"),
        _pair("R1", notes=f"{REJECTED_NOTE_PREFIX}: no"),
        _pair("C2", verified=True, created_at="2026-11-02T00:00:00+00:00"),
    ]
    ordered = [p["pair_id"] for p in prioritize_pairs_for_scan(pairs)]
    assert ordered == ["C2", "C1", "P1"]
    many = [_pair(f"X{i}") for i in range(40)]
    assert len(prioritize_pairs_for_scan(many)) == 25


def test_rejected_record_clears_all_attestations():
    rec = build_rejected_record(_pair("V", verified=True), "  Different   resolution source ")
    for flag in (
        "resolution_source_verified",
        "predicate_verified",
        "polarity_inverted",
        "void_rules_compatible",
        "allow_early_close_variance",
        "identical_settlement_verified",
    ):
        assert rec[flag] is False
    assert rec["settlement_status"] == "MISMATCH_BLOCKED"
    assert rec["notes"] == f"{REJECTED_NOTE_PREFIX}: Different resolution source"
    assert pair_review_state(rec) == "REJECTED"


def test_build_pair_review_reports_unavailable_markets_without_raising():
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={}))

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await build_pair_review(_pair("Z"), http_client=client)

    out = asyncio.run(run())
    assert out["polymarket"] is None and out["kalshi"] is None
    assert out["assessment"] is None
    assert len(out["errors"]) == 2
    assert out["review_state"] == "PENDING_REVIEW"


def test_build_pair_review_side_by_side_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/markets/POLY-Q" in path:
            return httpx.Response(200, json={
                "id": "POLY-Q", "question": "Will X happen?", "description": "Resolves YES if X.",
                "resolutionSource": "Official X", "endDate": "2026-12-31T00:00:00Z", "slug": "will-x",
            })
        if path.endswith("/markets/KX-Q"):
            return httpx.Response(200, json={"market": {
                "ticker": "KX-Q", "event_ticker": "KXEV", "title": "X happens?",
                "rules_primary": "If X, YES.", "can_close_early": True,
                "early_close_condition": "Closes early if X occurs.",
            }})
        if path.endswith("/events/KXEV"):
            return httpx.Response(200, json={"event": {
                "series_ticker": "KXSER", "title": "X event",
                "settlement_sources": [{"name": "Official X", "url": "https://x.example"}],
            }})
        if path.endswith("/series/KXSER"):
            return httpx.Response(200, json={"series": {"contract_url": "https://kalshi.example/x.pdf"}})
        return httpx.Response(404, json={})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await build_pair_review(_pair("Q"), http_client=client)

    out = asyncio.run(run())
    assert out["polymarket"]["rules_text"] == "Resolves YES if X."
    assert out["polymarket"]["url"] == "https://polymarket.com/market/will-x"
    assert out["kalshi"]["rules_primary"] == "If X, YES."
    assert out["kalshi"]["settlement_sources"] == [{"name": "Official X", "url": "https://x.example"}]
    assert out["kalshi"]["contract_url"] == "https://kalshi.example/x.pdf"
    assert out["kalshi"]["url"] == "https://kalshi.com/markets/kxser"


def test_review_and_reject_endpoints():
    app = create_app(db_path=":memory:")
    with TestClient(app) as client:
        # Reject requires an authenticated operator.
        assert client.post("/api/cross-venue/pairs/ANY/reject", json={"reason": "nope"}).status_code in (401, 403)

        authenticate_test_client(client)
        app.state.store.upsert_cross_venue_pair(_pair("E1"))

        listed = client.get("/api/cross-venue/pairs").json()
        items = listed.get("pairs", listed.get("items", listed))
        assert any(p["pair_id"] == "E1" and p["review_state"] == "PENDING_REVIEW" for p in items)

        assert client.get("/api/cross-venue/pairs/UNKNOWN/review").status_code == 404
        assert client.post("/api/cross-venue/pairs/UNKNOWN/reject", json={"reason": "nope"}).status_code == 404

        rej = client.post("/api/cross-venue/pairs/E1/reject", json={"reason": "Different predicate"})
        assert rej.status_code == 200
        assert rej.json()["pair"]["review_state"] == "REJECTED"
        stored = app.state.store.get_cross_venue_pair("E1")
        assert stored["identical_settlement_verified"] is False
        assert stored["notes"].startswith(REJECTED_NOTE_PREFIX)
