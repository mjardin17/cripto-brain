"""Six-stage paper-only orchestration with explicit, auditable handoffs."""
from __future__ import annotations
import math
from typing import Protocol
from .filters import evaluate_candidate
from .models import AnalystAssessment, Candidate, DeskConfig, DeskEvent, FilterEvidence, PaperPosition
from .risk import calculate_position_notional, update_position_risk

class Analyst(Protocol):
    def assess(self, candidate: Candidate, evidence: FilterEvidence) -> AnalystAssessment: ...

class SolanaPaperDesk:
    """No wallet, private key, Jupiter swap, or live execution path exists here."""
    def __init__(self, config: DeskConfig | None = None) -> None:
        self.config = config or DeskConfig()
        self.balance_usd = self.config.starting_balance_usd
        self.positions: dict[str, PaperPosition] = {}
        self.events: list[DeskEvent] = []
        self._seen_mints: set[str] = set()
        self.live_trading_enabled = False

    def _log(self, agent: str, event: str, mint: str | None = None, **details) -> None:
        self.events.append(DeskEvent(agent=agent, event=event, mint=mint, details=details))

    def process_candidate(self, candidate: Candidate, evidence: FilterEvidence, analyst: Analyst) -> dict:
        self._log("SCOUT", "CANDIDATE_DISCOVERED", candidate.mint, symbol=candidate.symbol)
        if candidate.mint in self._seen_mints:
            self._log("FILTER", "REJECT_DUPLICATE_MINT", candidate.mint)
            return {"status": "REJECTED", "reason": "DUPLICATE_MINT"}
        decision = evaluate_candidate(candidate, evidence, self.config)
        if not decision.passed:
            self._log("FILTER", "REJECTED", candidate.mint, reasons=decision.rejection_reasons)
            return {"status": "REJECTED", "agent": "FILTER", "reasons": decision.rejection_reasons}
        self._log("FILTER", "PASSED", candidate.mint)
        try:
            assessment = analyst.assess(candidate, evidence)
            if not math.isfinite(assessment.score) or not 0 <= assessment.score <= 1:
                raise ValueError("Analyst score outside [0, 1]")
        except Exception as exc:
            self._log("ANALYST", "FAILED_CLOSED", candidate.mint, error_type=type(exc).__name__)
            return {"status": "REJECTED", "agent": "ANALYST", "reason": "UNVERIFIED_ANALYST_RESULT"}
        self._log("ANALYST", "ASSESSED", candidate.mint, score=assessment.score,
                  rationale=assessment.rationale, evidence_ids=assessment.evidence_ids)
        if assessment.score < self.config.min_analyst_score:
            self._log("EXECUTOR", "NO_TRADE_SCORE_BELOW_THRESHOLD", candidate.mint, score=assessment.score)
            return {"status": "NO_TRADE", "score": assessment.score}
        if candidate.mint in self._seen_mints or candidate.mint in self.positions:
            self._log("EXECUTOR", "NO_TRADE_ALREADY_ENTERED", candidate.mint)
            return {"status": "NO_TRADE", "reason": "ONE_ENTRY_PER_MINT"}
        notional = calculate_position_notional(self.balance_usd, self.config)
        if notional <= 0 or notional > self.balance_usd:
            self._log("EXECUTOR", "NO_TRADE_INSUFFICIENT_BALANCE", candidate.mint)
            return {"status": "NO_TRADE", "reason": "INSUFFICIENT_BALANCE"}
        self._seen_mints.add(candidate.mint)
        self._log("EXECUTOR", "APPROVED_FOR_PAPER_QUOTE", candidate.mint,
                  max_notional_usd=notional, live_trading_enabled=False)
        self._log("LOGGER", "CANDIDATE_PIPELINE_COMPLETE", candidate.mint)
        return {"status": "PAPER_QUOTE_REQUIRED", "max_notional_usd": notional,
                "score": assessment.score, "live_trading_enabled": False}

    def open_paper_position(self, candidate: Candidate, entry_price: float, notional_usd: float) -> PaperPosition:
        if self.live_trading_enabled:
            raise RuntimeError("Live trading is forbidden in this module")
        if candidate.mint in self.positions or candidate.mint in self._seen_mints:
            raise ValueError("Only one entry per mint is permitted")
        max_notional = calculate_position_notional(self.balance_usd, self.config)
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("Entry price must be finite and positive")
        if not math.isfinite(notional_usd) or not 0 < notional_usd <= max_notional:
            raise ValueError("Paper notional exceeds the strict 8% balance cap")
        if notional_usd > self.balance_usd:
            raise ValueError("Insufficient paper balance")
        position = PaperPosition(mint=candidate.mint, symbol=candidate.symbol, entry_price=entry_price,
            mark_price=entry_price, peak_price=entry_price, notional_usd=notional_usd)
        self.balance_usd = round(self.balance_usd - notional_usd, 2)
        self.positions[candidate.mint] = position
        self._seen_mints.add(candidate.mint)
        self._log("EXECUTOR", "PAPER_BUY_FILLED", candidate.mint, entry_price=entry_price, notional_usd=notional_usd)
        return position

    def mark_positions(self, prices: dict[str, float]) -> list[dict]:
        outcomes: list[dict] = []
        for mint, position in list(self.positions.items()):
            updated, exit_reason = update_position_risk(position, prices.get(mint, float("nan")), self.config)
            if exit_reason is None:
                self.positions[mint] = updated
                continue
            pnl_fraction = (updated.mark_price - updated.entry_price) / updated.entry_price
            exit_value = updated.notional_usd * (1.0 + pnl_fraction)
            self.balance_usd = round(self.balance_usd + max(0.0, exit_value), 2)
            del self.positions[mint]
            self._log("RISK", "PAPER_POSITION_CLOSED", mint, reason=exit_reason,
                      pnl_fraction=pnl_fraction, exit_value_usd=exit_value)
            outcomes.append({"mint": mint, "reason": exit_reason, "pnl_fraction": pnl_fraction})
        return outcomes
