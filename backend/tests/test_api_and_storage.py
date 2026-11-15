"""Integration tests for FastAPI endpoints, DuckDB persistence, and Gamma market parser."""

from __future__ import annotations

import os

os.environ["CERTAINTY_DUCKDB_PATH"] = ":memory:"

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.tests.auth_helpers import authenticate_test_client
from backend.venues.polymarket import parse_gamma_binary_market


def test_parse_gamma_binary_market_filters_non_binary_and_extracts_tokens():
    valid_raw = {
        "id": "559651",
        "conditionId": "0xabc",
        "question": "Sample binary question?",
        "slug": "sample-binary",
        "endDate": "2027-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["111", "222"]',
        "enableOrderBook": True,
        "acceptingOrders": True,
        "feesEnabled": True,
        "feeType": "politics_fees",
        "feeSchedule": {"exponent": 1, "rate": 0.04, "takerOnly": True},
        "negRisk": False,
        "negRiskOther": False,
        "volume24hr": 12000,
        "liquidity": "50000",
    }
    parsed = parse_gamma_binary_market(valid_raw)
    assert parsed is not None
    assert parsed.yes_token_id == "111"
    assert parsed.no_token_id == "222"
    assert parsed.fee_rate == 0.04
    assert parsed.identical_settlement_verified is True

    # Non-binary market must be rejected
    multi_raw = dict(valid_raw, outcomes='["A", "B", "C"]', clobTokenIds='["1", "2", "3"]')
    assert parse_gamma_binary_market(multi_raw) is None


def test_fastapi_risk_gate_and_kill_switch_persistence(tmp_path):
    db_file = tmp_path / "test_certainty.duckdb"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as client:
        authenticate_test_client(client)
        h = client.get("/api/health?probe_venues=false")
        assert h.status_code == 200
        body = h.json()
        assert body["live_trading_enabled"] is False
        assert body["risk_gate"]["fail_closed"] is True

        # Evaluate a passing request
        eval_resp = client.post(
            "/api/risk-gate/evaluate",
            json={
                "venue": "polymarket",
                "arbitrage_type": "intra_venue",
                "strategy_tier": "tier1",
                "execution_mode": "PAPER_RESEARCH",
                "market_id": "M-100",
                "requested_notional_usd": 50.0,
                "current_venue_exposure_usd": 0.0,
                "daily_tier_loss_usd": 0.0,
                "net_edge_pct": 2.4,
                "liquidity_participation_pct": 4.0,
                "data_age_ms": 500,
                "leg_imbalance_ratio": 1.1,
                "executable": True,
                "identical_settlement_verified": True,
                "partial_fill_protection_enabled": True,
            },
        )
        assert eval_resp.status_code == 200
        assert eval_resp.json()["approved"] is True

        # Activate kill switch and verify fail-closed denial
        ks = client.post(
            "/api/risk-gate/kill-switch",
            json={"active": True, "reason": "Integration test emergency stop", "actor": "pytest"},
        )
        assert ks.status_code == 200
        assert ks.json()["active"] is True

        denied_resp = client.post(
            "/api/risk-gate/evaluate",
            json={
                "venue": "polymarket",
                "arbitrage_type": "intra_venue",
                "strategy_tier": "tier1",
                "execution_mode": "PAPER_RESEARCH",
                "market_id": "M-100",
                "requested_notional_usd": 50.0,
                "current_venue_exposure_usd": 0.0,
                "daily_tier_loss_usd": 0.0,
                "net_edge_pct": 5.0,
                "liquidity_participation_pct": 2.0,
                "data_age_ms": 200,
                "leg_imbalance_ratio": 1.0,
                "executable": True,
                "identical_settlement_verified": True,
                "partial_fill_protection_enabled": True,
            },
        )
        assert denied_resp.status_code == 200
        assert denied_resp.json()["approved"] is False
        assert "KILL_SWITCH" in denied_resp.json()["failed_rules"]

        audits = client.get("/api/risk-gate/audit").json()
        assert audits["count"] == 2


def test_scanner_enforces_consistent_canonical_data_age_fail_closed():
    import asyncio
    import time
    from backend.arbitrage.calculator import normalize_clob_book
    from backend.arbitrage.scanner import PolymarketArbitrageScanner
    from backend.risk_gate.engine import DeterministicRiskGate
    from backend.risk_gate.policy import RiskPolicy
    from backend.storage.duckdb_store import DuckDBStore
    from backend.venues.polymarket import PolymarketBinaryMarket, PolymarketBookSnapshot

    class _MockStaleClient:
        async def fetch_active_binary_markets(self, limit: int = 12):
            return [
                PolymarketBinaryMarket(
                    market_id="M-STALE-1",
                    condition_id="0xstale",
                    question="Will stale book fail closed?",
                    slug="stale-book",
                    end_date="2027-01-01T00:00:00Z",
                    yes_token_id="Y1",
                    no_token_id="N1",
                    fees_enabled=False,
                    fee_type="fee_free",
                    fee_rate=0.0,
                    fee_exponent=1.0,
                    neg_risk=False,
                    neg_risk_other=False,
                    volume_24hr=1000.0,
                    liquidity_usd=5000.0,
                    identical_settlement_verified=True,
                )
            ]

        async def fetch_order_book(self, token_id: str):
            now_ms = int(time.time() * 1000)
            price = "0.45" if token_id == "Y1" else "0.50"
            # Book timestamp is 25s stale even though HTTP fetch happened just now
            return PolymarketBookSnapshot(
                token_id=token_id,
                market_condition_id="0xstale",
                timestamp_ms=now_ms - 25_000,
                fetched_at_ms=now_ms,
                min_order_size=1.0,
                tick_size=0.01,
                neg_risk=False,
                book=normalize_clob_book([], [{"price": price, "size": "1000"}]),
                raw_bids_count=0,
                raw_asks_count=1,
            )

    store = DuckDBStore(db_path=":memory:")
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    scanner = PolymarketArbitrageScanner(store=store, risk_gate=gate, client=_MockStaleClient())  # type: ignore[arg-type]

    scan_res = asyncio.run(scanner.scan(limit=1, target_shares=25.0))
    assert scan_res["status"] == "COMPLETED"
    assert scan_res["approved_count"] == 0
    opp = scan_res["opportunities"][0]
    assert opp["data_age_ms"] >= 24_900
    assert opp["risk_gate_approved"] is False
    assert "FRESH_MARKET_DATA" in opp["risk_gate_reason"]

    audit = store.list_risk_audit(limit=1)[0]
    snap = store.get_latest_snapshot_for_market("M-STALE-1")
    assert snap is not None
    assert audit["data_age_ms"] == opp["data_age_ms"]
    assert snap["book_payload"]["evaluation"]["data_age_ms"] == opp["data_age_ms"]
    store.close()
