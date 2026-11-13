"""Integration tests for FastAPI endpoints, DuckDB persistence, and Gamma market parser."""

from __future__ import annotations

import os

os.environ["CERTAINTY_DUCKDB_PATH"] = ":memory:"

from fastapi.testclient import TestClient

from backend.main import create_app
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
