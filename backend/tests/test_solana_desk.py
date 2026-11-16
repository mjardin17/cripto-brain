"""Tests for fail-closed Solana memecoin paper desk primitives."""
import pytest
from backend.solana_desk.engine import SolanaPaperDesk
from backend.solana_desk.filters import evaluate_candidate
from backend.solana_desk.models import AnalystAssessment, Candidate, FilterEvidence, PaperPosition
from backend.solana_desk.risk import calculate_position_notional, update_position_risk

class FixedAnalyst:
    def __init__(self, score=0.75): self.score = score
    def assess(self, candidate, evidence):
        return AnalystAssessment(score=self.score, rationale="test assessment")

def good_evidence(**overrides):
    data = dict(mint_authority_revoked=True, freeze_authority_revoked=True, lp_locked_pct=98.0,
        top10_holders_pct=12.0, volume_1h_usd=80000.0, sell_simulation_passed=True,
        dev_wallet_clear=True, evidence_sources=["test-fixture"])
    data.update(overrides)
    return FilterEvidence(**data)

def candidate(mint="So11111111111111111111111111111111111111112"):
    return Candidate(mint=mint, symbol="TEST")

def test_filter_passes_only_complete_safe_evidence():
    assert evaluate_candidate(candidate(), good_evidence()).passed
    decision = evaluate_candidate(candidate(), FilterEvidence())
    assert not decision.passed
    assert len(decision.rejection_reasons) == 7

@pytest.mark.parametrize("override", [
    {"mint_authority_revoked": False}, {"freeze_authority_revoked": False},
    {"lp_locked_pct": 94.99}, {"top10_holders_pct": 20.01}, {"volume_1h_usd": 49999.99},
    {"sell_simulation_passed": False}, {"dev_wallet_clear": False},
])
def test_each_filter_rule_vetoes(override):
    assert not evaluate_candidate(candidate(), good_evidence(**override)).passed

def test_position_size_is_exactly_capped_at_eight_percent():
    assert calculate_position_notional(280.0) == 22.40
    assert calculate_position_notional(100.0) == 8.0

def test_analyst_error_fails_closed():
    class Broken:
        def assess(self, candidate, evidence): raise RuntimeError("provider unavailable")
    desk = SolanaPaperDesk()
    result = desk.process_candidate(candidate(), good_evidence(), Broken())
    assert result["status"] == "REJECTED"
    assert result["reason"] == "UNVERIFIED_ANALYST_RESULT"
    assert desk.balance_usd == 280.0

def test_score_below_threshold_never_creates_position():
    desk = SolanaPaperDesk()
    result = desk.process_candidate(candidate(), good_evidence(), FixedAnalyst(0.59))
    assert result["status"] == "NO_TRADE"
    assert desk.positions == {}

def test_approved_candidate_only_advances_to_quote_not_buy():
    desk = SolanaPaperDesk()
    result = desk.process_candidate(candidate(), good_evidence(), FixedAnalyst())
    assert result["status"] == "PAPER_QUOTE_REQUIRED"
    assert result["max_notional_usd"] == 22.40
    assert desk.positions == {}
    assert desk.live_trading_enabled is False

def test_hard_stop_and_trailing_stop():
    pos = PaperPosition(mint="mint", symbol="T", entry_price=100, mark_price=100, peak_price=100, notional_usd=8)
    _, reason = update_position_risk(pos, 94.0)
    assert reason == "HARD_STOP_6_PERCENT"
    updated, reason = update_position_risk(pos, 112.0)
    assert updated.trailing_active is True
    assert reason is None
    _, reason = update_position_risk(updated, 105.0)
    assert reason == "TRAILING_STOP_6_PERCENT_FROM_PEAK"

def test_one_entry_per_mint():
    desk = SolanaPaperDesk()
    c = candidate()
    desk.open_paper_position(c, 1.0, 22.4)
    with pytest.raises(ValueError):
        desk.open_paper_position(c, 0.9, 10.0)
