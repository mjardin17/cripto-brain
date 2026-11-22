"""Stage 2C tests: deterministic paper execution simulator, Risk Gate locks, latency drift, unwinds, and DuckDB blotter."""

from __future__ import annotations

import asyncio
import time
import pytest
from fastapi.testclient import TestClient

from backend.arbitrage.calculator import normalize_clob_book
from backend.execution.paper_simulator import PaperExecutionEngine, PaperExecutionRequest
from backend.main import create_app
from backend.tests.auth_helpers import authenticate_test_client
from backend.risk_gate.engine import DeterministicRiskGate
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import KalshiBinaryMarket, KalshiBookSnapshot, normalize_kalshi_orderbook
from backend.venues.polymarket import PolymarketBinaryMarket, PolymarketBookSnapshot


def _poly_market(**overrides) -> PolymarketBinaryMarket:
    base = dict(
        market_id="POLY-CPI-26",
        condition_id="0xcondcpi26",
        question="Will US CPI exceed 3.0% in Dec 2026?",
        slug="us-cpi-dec-2026",
        end_date="2026-12-20T14:00:00Z",
        yes_token_id="POLY-Y",
        no_token_id="POLY-N",
        fees_enabled=True,
        fee_type="economics",
        fee_rate=0.02,
        fee_exponent=1.0,
        neg_risk=False,
        neg_risk_other=False,
        volume_24hr=60000.0,
        liquidity_usd=30000.0,
        identical_settlement_verified=True,
    )
    base.update(overrides)
    return PolymarketBinaryMarket(**base)


def _kalshi_market(**overrides) -> KalshiBinaryMarket:
    base = dict(
        ticker="KXCPI-26DEC-T30",
        event_ticker="KXCPI-26DEC",
        series_ticker="KXCPI",
        title="Will CPI exceed 3.0% in December 2026?",
        subtitle="",
        yes_sub_title="Above 3.0%",
        no_sub_title="3.0% or below",
        rules_primary="Resolves Yes if official BLS CPI exceeds 3.0%.",
        rules_secondary="",
        open_time="2026-11-01T14:00:00Z",
        close_time="2026-12-20T13:30:00Z",
        expiration_time="2026-12-20T13:30:00Z",
        updated_time="2026-11-12T23:00:00Z",
        updated_at_ms=1794553000000,
        can_close_early=False,
        yes_bid_dollars=0.48,
        yes_ask_dollars=0.52,
        no_bid_dollars=0.48,
        no_ask_dollars=0.52,
        volume_24h=15000.0,
        open_interest=45000.0,
    )
    base.update(overrides)
    return KalshiBinaryMarket(**base)


class _StubPolyClient:
    def __init__(self, yes_ask: float = 0.42, yes_bid: float = 0.39, no_ask: float = 0.60):
        self.yes_ask = yes_ask
        self.yes_bid = yes_bid
        self.no_ask = no_ask
        self.market = _poly_market()

    async def fetch_market(self, market_id: str):
        return self.market if market_id == self.market.market_id else None

    async def fetch_order_book(self, token_id: str) -> PolymarketBookSnapshot:
        now_ms = int(time.time() * 1000)
        if token_id == "POLY-Y":
            book = normalize_clob_book(
                [{"price": str(self.yes_bid), "size": "1000"}, {"price": str(round(self.yes_bid - 0.02, 4)), "size": "1000"}],
                [{"price": str(self.yes_ask), "size": "1000"}],
            )
        else:
            book = normalize_clob_book(
                [{"price": "0.35", "size": "1000"}],
                [{"price": str(self.no_ask), "size": "1000"}],
            )
        return PolymarketBookSnapshot(
            token_id=token_id,
            market_condition_id=self.market.condition_id,
            timestamp_ms=now_ms - 150,
            fetched_at_ms=now_ms,
            min_order_size=1.0,
            tick_size=0.01,
            neg_risk=False,
            book=book,
            raw_bids_count=2,
            raw_asks_count=1,
        )


class _StubKalshiClient:
    def __init__(self, yes_bid: float = 0.48, no_bid: float = 0.48):
        # no_ask = 1.00 - yes_bid (default 0.52), yes_ask = 1.00 - no_bid (default 0.52)
        self.yes_bid = yes_bid
        self.no_bid = no_bid
        self.market = _kalshi_market()

    async def fetch_market(self, ticker: str):
        return self.market if ticker == self.market.ticker else None

    async def fetch_order_book(self, ticker: str, depth: int = 20, book_timestamp_ms=None) -> KalshiBookSnapshot:
        now_ms = int(time.time() * 1000)
        ky, kn, _, _ = normalize_kalshi_orderbook({
            "orderbook_fp": {
                "yes_dollars": [[f"{self.yes_bid:.4f}", "800.00"]],
                "no_dollars": [[f"{self.no_bid:.4f}", "800.00"]],
            }
        })
        return KalshiBookSnapshot(
            ticker=ticker,
            timestamp_ms=now_ms - 100,
            fetched_at_ms=now_ms,
            yes_book=ky,
            no_book=kn,
            raw_yes_bids_count=1,
            raw_no_bids_count=1,
        )


def _verified_pair() -> dict:
    return {
        "pair_id": "XPAIR-CPI26",
        "poly_market_id": "POLY-CPI-26",
        "poly_question": "Will US CPI exceed 3.0% in Dec 2026?",
        "kalshi_ticker": "KXCPI-26DEC-T30",
        "kalshi_title": "Will CPI exceed 3.0% in December 2026?",
        "resolution_source_verified": True,
        "predicate_verified": True,
        "polarity_inverted": False,
        "void_rules_compatible": True,
        "allow_early_close_variance": False,
        "identical_settlement_verified": True,
        "settlement_status": "VERIFIED",
        "mismatch_reasons": [],
        "cutoff_delta_hours": 0.5,
        "notes": "Verified test pair",
        "created_at": utc_now_iso(),
    }


def test_paper_execution_requires_risk_gate_and_persists_clean_hedged_fill():
    store = DuckDBStore(db_path=":memory:")
    pair = _verified_pair()
    store.upsert_cross_venue_pair(pair)
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    engine = PaperExecutionEngine(
        store=store,
        risk_gate=gate,
        poly_client=_StubPolyClient(),  # type: ignore[arg-type]
        kalshi_client=_StubKalshiClient(),  # type: ignore[arg-type]
    )

    # AI override attempt must fail closed before any paper order is created
    denied_ai = asyncio.run(
        engine.execute(PaperExecutionRequest(pair_id="XPAIR-CPI26", target_shares=25.0, ai_suggested_override=True))
    )
    assert denied_ai["status"] == "REJECTED_BY_RISK_GATE"
    assert denied_ai["approved"] is False
    assert denied_ai["orders"] == []
    assert store.get_counts()["paper_orders"] == 0

    # Clean execution -> dual hedged fills, zero realized profit before settlement, locked carry recorded
    res = asyncio.run(
        engine.execute(PaperExecutionRequest(pair_id="XPAIR-CPI26", target_shares=25.0, scenario="LIVE_BOOKS"))
    )
    assert res["approved"] is True
    assert res["status"] == "HEDGED_FULL_FILL"
    assert res["provenance"] == "SIMULATED"
    assert res["live_trading_enabled"] is False
    assert len(res["orders"]) == 2
    assert len(res["fills"]) == 2
    assert len(res["positions"]) == 2
    assert all(p["status"] == "OPEN_HEDGED" for p in res["positions"])
    assert res["pnl_entry"]["realized_pnl_usd"] == 0.0
    assert res["pnl_entry"]["locked_arb_edge_usd"] > 0.5

    exposures = store.get_paper_venue_exposures()
    assert exposures["polymarket"] > 10.0
    assert exposures["kalshi"] > 12.0
    store.close()


def test_partial_fill_on_leg1_caps_leg2_and_avoids_overhedging():
    store = DuckDBStore(db_path=":memory:")
    store.upsert_cross_venue_pair(_verified_pair())
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    engine = PaperExecutionEngine(
        store=store,
        risk_gate=gate,
        poly_client=_StubPolyClient(),  # type: ignore[arg-type]
        kalshi_client=_StubKalshiClient(),  # type: ignore[arg-type]
    )

    res = asyncio.run(
        engine.execute(
            PaperExecutionRequest(
                pair_id="XPAIR-CPI26",
                target_shares=25.0,
                scenario="PARTIAL_FILL_LEG1",
                partial_fill_ratio=0.4,
            )
        )
    )
    assert res["approved"] is True
    assert res["status"] == "HEDGED_PARTIAL_LEG1"
    assert res["orders"][0]["status"] == "PARTIALLY_FILLED"
    assert res["orders"][0]["filled_shares"] == pytest.approx(10.0)
    assert res["orders"][1]["requested_shares"] == pytest.approx(10.0)
    assert res["orders"][1]["filled_shares"] == pytest.approx(10.0)
    assert res["pnl_entry"]["matched_shares"] == pytest.approx(10.0)
    assert res["pnl_entry"]["unwound_shares"] == pytest.approx(0.0)
    store.close()


def test_second_leg_drift_and_rejection_trigger_bid_unwind_loss():
    store = DuckDBStore(db_path=":memory:")
    store.upsert_cross_venue_pair(_verified_pair())
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    engine = PaperExecutionEngine(
        store=store,
        risk_gate=gate,
        poly_client=_StubPolyClient(),  # type: ignore[arg-type]
        kalshi_client=_StubKalshiClient(),  # type: ignore[arg-type]
    )

    drift_res = asyncio.run(
        engine.execute(
            PaperExecutionRequest(
                pair_id="XPAIR-CPI26",
                target_shares=25.0,
                scenario="SECOND_LEG_PRICE_DRIFT_UNWIND",
            )
        )
    )
    assert drift_res["status"] == "UNWOUND_SECOND_LEG_FAILURE"
    assert drift_res["orders"][1]["status"] == "CANCELLED_LIMIT_BREACH"
    assert drift_res["orders"][2]["side"] == "SELL"
    assert drift_res["orders"][2]["status"] == "UNWIND_FILLED"
    assert any(f["is_unwind"] is True for f in drift_res["fills"])
    assert drift_res["pnl_entry"]["realized_pnl_usd"] < 0.0
    assert store.get_daily_tier1_simulated_loss_usd() > 0.0
    # Fully unwound position leaves 0 active exposure
    exposures = store.get_paper_venue_exposures()
    assert exposures["polymarket"] == 0.0
    assert exposures["kalshi"] == 0.0

    reject_res = asyncio.run(
        engine.execute(
            PaperExecutionRequest(
                pair_id="XPAIR-CPI26",
                target_shares=25.0,
                scenario="LEG2_REJECTED_UNWIND",
            )
        )
    )
    assert reject_res["status"] == "UNWOUND_SECOND_LEG_FAILURE"
    assert reject_res["orders"][1]["status"] == "REJECTED_SECOND_LEG"
    assert reject_res["pnl_entry"]["realized_pnl_usd"] < 0.0
    store.close()


def test_daily_loss_limit_and_venue_exposure_caps_block_paper_execution():
    store = DuckDBStore(db_path=":memory:")
    store.upsert_cross_venue_pair(_verified_pair())
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    engine = PaperExecutionEngine(
        store=store,
        risk_gate=gate,
        poly_client=_StubPolyClient(),  # type: ignore[arg-type]
        kalshi_client=_StubKalshiClient(),  # type: ignore[arg-type]
    )

    # Record $255 simulated daily loss directly in ledger -> exceeds $250 Tier 1 limit
    store.record_paper_pnl({
        "ledger_id": "PPNL-LOSS-LIMIT",
        "execution_id": "PEXEC-OLD",
        "pair_id": "XPAIR-CPI26",
        "event_type": "UNWIND_LOSS",
        "outcome_status": "UNWOUND_SECOND_LEG_FAILURE",
        "matched_shares": 0.0,
        "unwound_shares": 100.0,
        "gross_cost_usd": 42.0,
        "fees_usd": 5.0,
        "realized_pnl_usd": -255.0,
        "locked_arb_edge_usd": 0.0,
        "notes": "Simulated cumulative loss breach",
        "provenance": "SIMULATED",
        "recorded_at": utc_now_iso(),
    })

    blocked_loss = asyncio.run(
        engine.execute(PaperExecutionRequest(pair_id="XPAIR-CPI26", target_shares=25.0))
    )
    assert blocked_loss["status"] == "REJECTED_BY_RISK_GATE"
    assert "DAILY_LOSS_LIMIT" in blocked_loss["gate_result"]["failed_rules"]
    assert store.get_counts()["paper_orders"] == 0
    store.close()

    # Now verify $2,500 per-venue exposure cap
    store2 = DuckDBStore(db_path=":memory:")
    store2.upsert_cross_venue_pair(_verified_pair())
    gate2 = DeterministicRiskGate(store=store2, policy=RiskPolicy())
    engine2 = PaperExecutionEngine(
        store=store2,
        risk_gate=gate2,
        poly_client=_StubPolyClient(),  # type: ignore[arg-type]
        kalshi_client=_StubKalshiClient(),  # type: ignore[arg-type]
    )
    store2.record_paper_position({
        "position_id": "PPOS-HEAVY",
        "execution_id": "PEXEC-EXISTING",
        "pair_id": "XPAIR-CPI26",
        "venue": "kalshi",
        "market_ref": "KXCPI-26DEC-T30",
        "outcome": "NO",
        "shares": 4000.0,
        "entry_price": 0.623,
        "notional_usd": 2495.0,
        "locked_payout_usd": 2000.0,
        "status": "OPEN_HEDGED",
        "provenance": "SIMULATED",
        "updated_at": utc_now_iso(),
    })

    blocked_cap = asyncio.run(
        engine2.execute(PaperExecutionRequest(pair_id="XPAIR-CPI26", target_shares=25.0))
    )
    assert blocked_cap["status"] == "REJECTED_BY_RISK_GATE"
    assert "MAX_VENUE_EXPOSURE" in blocked_cap["gate_result"]["failed_rules"]
    assert store2.get_counts()["paper_orders"] == 0
    store2.close()


def test_fastapi_cross_venue_and_paper_blotter_routes():
    app = create_app(db_path=":memory:")
    with TestClient(app) as client:
        authenticate_test_client(client)
        # Swap network clients on app.state with deterministic test stubs
        app.state.poly_client = _StubPolyClient()
        app.state.kalshi_client = _StubKalshiClient()
        app.state.cross_scanner.poly_client = app.state.poly_client
        app.state.cross_scanner.kalshi_client = app.state.kalshi_client
        app.state.paper_engine.poly_client = app.state.poly_client
        app.state.paper_engine.kalshi_client = app.state.kalshi_client

        # Register verified pair via API
        reg = client.post(
            "/api/cross-venue/pairs",
            json={
                "pair_id": "XPAIR-API-TEST",
                "poly_market_id": "POLY-CPI-26",
                "kalshi_ticker": "KXCPI-26DEC-T30",
                "resolution_source_verified": True,
                "predicate_verified": True,
                "void_rules_compatible": True,
            },
        )
        assert reg.status_code == 200
        assert reg.json()["pair"]["identical_settlement_verified"] is True

        # Scan pair via API
        scan = client.post("/api/cross-venue/scan", json={"pair_id": "XPAIR-API-TEST", "target_shares": 25.0})
        assert scan.status_code == 200
        assert scan.json()["count"] == 1
        assert scan.json()["results"][0]["risk_gate_approved"] is True

        # Execute paper unwind test via API
        exec_res = client.post(
            "/api/paper/execute",
            json={"pair_id": "XPAIR-API-TEST", "target_shares": 25.0, "scenario": "SECOND_LEG_PRICE_DRIFT_UNWIND"},
        )
        assert exec_res.status_code == 200
        assert exec_res.json()["status"] == "UNWOUND_SECOND_LEG_FAILURE"

        # Blotter reflects simulated fill & negative unwind P&L
        blotter = client.get("/api/paper/blotter")
        assert blotter.status_code == 200
        b_data = blotter.json()
        assert b_data["provenance"] == "SIMULATED"
        assert b_data["live_trading_enabled"] is False
        assert len(b_data["orders"]) == 3
        assert b_data["summary"]["daily_simulated_pnl_usd"] < 0.0

        # Dashboard updates simulated daily P&L and keeps live_trading_enabled False
        dash = client.get("/api/dashboard").json()
        assert dash["live_trading_enabled"] is False
        assert dash["portfolio"]["daily_simulated_pnl_usd"] < 0.0
        tiers = {m["module_id"]: m for m in dash["modules"]}
        assert tiers["paper_executor"]["status"] == "ACTIVE_SIMULATED"
        assert tiers["tier2_delta_neutral"]["status"] == "INACTIVE"
        assert tiers["tier3_ai_research"]["status"] == "INACTIVE"


def test_paper_blotter_manual_resolution_and_learning_loop():
    app = create_app(db_path=":memory:")
    with TestClient(app) as client:
        authenticate_test_client(client)
        app.state.poly_client = _StubPolyClient()
        app.state.kalshi_client = _StubKalshiClient()
        app.state.cross_scanner.poly_client = app.state.poly_client
        app.state.cross_scanner.kalshi_client = app.state.kalshi_client
        app.state.paper_engine.poly_client = app.state.poly_client
        app.state.paper_engine.kalshi_client = app.state.kalshi_client

        # Register verified pair & run two paper executions
        reg = client.post(
            "/api/cross-venue/pairs",
            json={
                "pair_id": "XPAIR-RES-TEST",
                "poly_market_id": "POLY-CPI-26",
                "kalshi_ticker": "KXCPI-26DEC-T30",
                "resolution_source_verified": True,
                "predicate_verified": True,
                "void_rules_compatible": True,
            },
        )
        assert reg.status_code == 200

        exec1 = client.post(
            "/api/paper/execute",
            json={"pair_id": "XPAIR-RES-TEST", "target_shares": 25.0, "scenario": "LIVE_BOOKS"},
        )
        assert exec1.status_code == 200
        ledger1_id = exec1.json()["pnl_entry"]["ledger_id"]

        exec2 = client.post(
            "/api/paper/execute",
            json={"pair_id": "XPAIR-RES-TEST", "target_shares": 10.0, "scenario": "LIVE_BOOKS"},
        )
        assert exec2.status_code == 200
        ledger2_id = exec2.json()["pnl_entry"]["ledger_id"]

        # Verify default PENDING resolution_result & summary counts
        blotter_before = client.get("/api/paper/blotter").json()
        assert blotter_before["summary"]["resolved_entries_count"] == 0
        assert blotter_before["summary"]["pending_resolution_count"] == 2
        by_id = {row["ledger_id"]: row for row in blotter_before["pnl_ledger"]}
        assert by_id[ledger1_id]["resolution_result"] == "PENDING"
        assert by_id[ledger2_id]["resolution_result"] == "PENDING"

        # Create an open Arena forecast prediction to link to ledger1
        pred_res = client.post(
            "/api/intelligence/predictions",
            json={
                "agent_id": "arena:gemini-2.5-pro",
                "market_id": "POLY-CPI-26",
                "venue": "polymarket",
                "subject_key": "cpi_dec_2026",
                "question": "Will US CPI exceed 3.0% in Dec 2026?",
                "predicted_prob": 0.80,
                "market_implied_prob": 0.55,
                "confidence": 0.78,
                "rationale": "Arena forecast for CPI threshold.",
                "provenance": "LIVE",
            },
        )
        assert pred_res.status_code == 200
        pred_id = pred_res.json()["prediction_id"]

        # Invalid enum rejected with 422, unknown ledger rejected with 404
        assert (
            client.post(
                f"/api/paper/blotter/{ledger1_id}/resolve",
                json={"resolution_result": "MAYBE"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/paper/blotter/PPNL-DOES-NOT-EXIST/resolve",
                json={"resolution_result": "YES"},
            ).status_code
            == 404
        )

        # Confirm actual market outcome YES with linked Arena forecast
        confirm1 = client.post(
            f"/api/paper/blotter/{ledger1_id}/resolve",
            json={
                "resolution_result": "YES",
                "notes": "Official BLS CPI release confirmed > 3.0%.",
                "linked_prediction_id": pred_id,
                "arena_model_id": "arena:gemini-2.5-pro",
            },
        )
        assert confirm1.status_code == 200, confirm1.text
        c1 = confirm1.json()
        assert c1["provenance"] == "SIMULATED"
        assert c1["live_trading_enabled"] is False
        assert c1["entry"]["resolution_result"] == "YES"
        assert c1["entry"]["resolved_by"] == "testowner"
        assert c1["entry"]["linked_prediction_id"] == pred_id
        assert c1["entry"]["arena_model_id"] == "arena:gemini-2.5-pro"
        assert c1["entry"]["learning_cycle_id"].startswith("LCYC-")
        assert c1["summary"]["resolved_entries_count"] == 1
        assert c1["summary"]["pending_resolution_count"] == 1
        assert c1["resolved_prediction"]["status"] == "RESOLVED"
        assert c1["resolved_prediction"]["actual_outcome"] == 1
        assert c1["resolved_prediction"]["brier_score"] == pytest.approx(0.04, abs=1e-4)
        assert c1["learning_cycle"]["stage"] == "OUTCOME"
        assert c1["learning_cycle"]["source_type"] == "PAPER_BLOTTER_RESOLUTION"
        assert c1["learning_cycle"]["source_ref_id"] == ledger1_id
        assert c1["learning_cycle"]["outcome_matched"] is True

        # Fail closed on duplicate resolution attempt
        dup = client.post(
            f"/api/paper/blotter/{ledger1_id}/resolve",
            json={"resolution_result": "NO"},
        )
        assert dup.status_code == 409

        # Confirm second entry as VOID -> recorded with outcome_matched=False
        confirm2 = client.post(
            f"/api/paper/blotter/{ledger2_id}/resolve",
            json={
                "resolution_result": "VOID",
                "notes": "Venue voided contract due to source ambiguity.",
            },
        )
        assert confirm2.status_code == 200
        c2 = confirm2.json()
        assert c2["entry"]["resolution_result"] == "VOID"
        assert c2["learning_cycle"]["outcome_matched"] is False
        assert c2["summary"]["resolved_entries_count"] == 2
        assert c2["summary"]["pending_resolution_count"] == 0


def test_paper_blotter_resolution_rbac_and_csrf_enforced():
    app = create_app(db_path=":memory:")
    with TestClient(app) as owner_client:
        authenticate_test_client(owner_client)
        app.state.store.record_paper_pnl({
            "ledger_id": "PPNL-RBAC-001",
            "execution_id": "PEXEC-RBAC-001",
            "pair_id": "XPAIR-RBAC",
            "event_type": "HEDGED_ENTRY",
            "outcome_status": "HEDGED_FULL_FILL",
            "matched_shares": 25.0,
            "unwound_shares": 0.0,
            "gross_cost_usd": 23.0,
            "fees_usd": 0.4,
            "realized_pnl_usd": 0.0,
            "locked_arb_edge_usd": 1.6,
            "notes": "RBAC verification row",
            "provenance": "SIMULATED",
            "recorded_at": utc_now_iso(),
        })

        # Create READ_ONLY, ANALYST, and OPERATOR users
        for uname, role in [
            ("ro_blotter", "READ_ONLY"),
            ("an_blotter", "ANALYST"),
            ("op_blotter", "OPERATOR"),
        ]:
            resp = owner_client.post(
                "/api/admin/users",
                json={
                    "username": uname,
                    "password": "Strong-Role-Pass!2026",
                    "role": role,
                    "email": f"{uname}@certainty.local",
                },
            )
            assert resp.status_code == 200, resp.text

        # Unauthenticated request rejected (401)
        anon = TestClient(app)
        assert (
            anon.post(
                "/api/paper/blotter/PPNL-RBAC-001/resolve",
                json={"resolution_result": "YES"},
            ).status_code
            == 401
        )

        # READ_ONLY and ANALYST rejected (403)
        for uname in ("ro_blotter", "an_blotter"):
            low_client = TestClient(app)
            authenticate_test_client(
                low_client, username=uname, password="Strong-Role-Pass!2026"
            )
            res = low_client.post(
                "/api/paper/blotter/PPNL-RBAC-001/resolve",
                json={"resolution_result": "YES"},
            )
            assert res.status_code == 403

        # OPERATOR without CSRF token rejected (403), then succeeds with CSRF token (200)
        op_client = TestClient(app)
        authenticate_test_client(
            op_client, username="op_blotter", password="Strong-Role-Pass!2026"
        )
        saved_csrf = op_client.headers.pop("X-CSRF-Token", None)
        no_csrf = op_client.post(
            "/api/paper/blotter/PPNL-RBAC-001/resolve",
            json={"resolution_result": "NO"},
        )
        assert no_csrf.status_code == 403

        if saved_csrf:
            op_client.headers["X-CSRF-Token"] = saved_csrf
        ok = op_client.post(
            "/api/paper/blotter/PPNL-RBAC-001/resolve",
            json={"resolution_result": "NO", "notes": "Confirmed by operator"},
        )
        assert ok.status_code == 200
        assert ok.json()["entry"]["resolution_result"] == "NO"
        assert ok.json()["entry"]["resolved_by"] == "op_blotter"

