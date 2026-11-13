"""Independent unit tests for the fail-closed Deterministic Risk Gate."""

from __future__ import annotations

import math

import pytest

from backend.risk_gate.engine import DeterministicRiskGate, RiskEvaluationRequest
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore


@pytest.fixture()
def gate_and_store():
    store = DuckDBStore(db_path=":memory:")
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    yield gate, store
    store.close()


def _valid_request(**overrides) -> RiskEvaluationRequest:
    base = {
        "venue": "polymarket",
        "arbitrage_type": "intra_venue",
        "strategy_tier": "tier1",
        "execution_mode": "PAPER_RESEARCH",
        "market_id": "559651",
        "requested_notional_usd": 100.0,
        "current_venue_exposure_usd": 0.0,
        "daily_tier_loss_usd": 0.0,
        "net_edge_pct": 2.15,
        "liquidity_participation_pct": 5.0,
        "data_age_ms": 400,
        "leg_imbalance_ratio": 1.2,
        "executable": True,
        "identical_settlement_verified": True,
        "partial_fill_protection_enabled": True,
        "ai_suggested_override": False,
    }
    base.update(overrides)
    return RiskEvaluationRequest(**base)


def test_approves_valid_intra_venue_opportunity(gate_and_store):
    gate, store = gate_and_store
    res = gate.evaluate(_valid_request())
    assert res.approved is True
    assert res.decision == "APPROVED"
    assert res.failed_rules == []
    audits = store.list_risk_audit(limit=10)
    assert len(audits) == 1
    assert audits[0]["decision"] == "APPROVED"


def test_denies_below_1_5_percent_intra_venue_edge(gate_and_store):
    gate, _ = gate_and_store
    res = gate.evaluate(_valid_request(net_edge_pct=1.49))
    assert res.approved is False
    assert "MIN_NET_EDGE" in res.failed_rules


def test_denies_excessive_liquidity_participation_above_10_pct(gate_and_store):
    gate, _ = gate_and_store
    res = gate.evaluate(_valid_request(liquidity_participation_pct=10.5))
    assert res.approved is False
    assert "LIQUIDITY_PARTICIPATION_CAP" in res.failed_rules


def test_denies_stale_market_data(gate_and_store):
    gate, _ = gate_and_store
    res = gate.evaluate(_valid_request(data_age_ms=20_000))
    assert res.approved is False
    assert "FRESH_MARKET_DATA" in res.failed_rules


def test_denies_live_execution_in_phase_1(gate_and_store):
    gate, _ = gate_and_store
    res = gate.evaluate(_valid_request(execution_mode="LIVE"))
    assert res.approved is False
    assert "LIVE_EXECUTION_LOCK" in res.failed_rules


def test_denies_ai_override_attempt(gate_and_store):
    gate, _ = gate_and_store
    res = gate.evaluate(_valid_request(ai_suggested_override=True))
    assert res.approved is False
    assert "NO_AI_OVERRIDE" in res.failed_rules


def test_denies_when_kill_switch_is_active(gate_and_store):
    gate, store = gate_and_store
    store.set_kill_switch(True, reason="Pytest kill switch verification")
    res = gate.evaluate(_valid_request())
    assert res.approved is False
    assert res.kill_switch_active is True
    assert "KILL_SWITCH" in res.failed_rules


def test_denies_inactive_tiers_and_unverified_settlement_and_nan(gate_and_store):
    gate, _ = gate_and_store
    r_tier2 = gate.evaluate(_valid_request(strategy_tier="tier2"))
    assert not r_tier2.approved and "STRATEGY_TIER_ACTIVE" in r_tier2.failed_rules

    r_settle = gate.evaluate(_valid_request(identical_settlement_verified=False))
    assert not r_settle.approved and "IDENTICAL_SETTLEMENT" in r_settle.failed_rules

    r_nan = gate.evaluate(_valid_request(net_edge_pct=math.nan))
    assert not r_nan.approved and "EXECUTABLE_FINITE_INPUTS" in r_nan.failed_rules


def test_denies_leg_imbalance_and_missing_partial_fill_guard(gate_and_store):
    gate, _ = gate_and_store
    res = gate.evaluate(_valid_request(leg_imbalance_ratio=8.5, partial_fill_protection_enabled=False))
    assert not res.approved
    assert "PARTIAL_FILL_AND_LEG_RISK" in res.failed_rules
