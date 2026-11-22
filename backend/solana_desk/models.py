"""Typed contracts for the six-agent Solana research pipeline."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mint: str = Field(min_length=32, max_length=64)
    symbol: str = Field(default="UNKNOWN", min_length=1, max_length=32)
    pair_address: str | None = None
    source: str = "unverified"
    discovered_at: datetime = Field(default_factory=utc_now)

class FilterEvidence(BaseModel):
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    lp_locked_pct: float | None = Field(default=None, ge=0, le=100)
    top10_holders_pct: float | None = Field(default=None, ge=0, le=100)
    volume_1h_usd: float | None = Field(default=None, ge=0)
    sell_simulation_passed: bool | None = None
    dev_wallet_clear: bool | None = None
    evidence_sources: list[str] = Field(default_factory=list)

class FilterDecision(BaseModel):
    candidate: Candidate
    passed: bool
    rejection_reasons: list[str] = Field(default_factory=list)
    evidence: FilterEvidence

class AnalystAssessment(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)

class PaperPosition(BaseModel):
    mint: str
    symbol: str
    entry_price: float = Field(gt=0)
    mark_price: float = Field(gt=0)
    peak_price: float = Field(gt=0)
    notional_usd: float = Field(gt=0)
    trailing_active: bool = False
    opened_at: datetime = Field(default_factory=utc_now)

class DeskEvent(BaseModel):
    timestamp: datetime = Field(default_factory=utc_now)
    agent: Literal["SCOUT", "FILTER", "ANALYST", "EXECUTOR", "RISK", "LOGGER"]
    event: str
    mint: str | None = None
    details: dict = Field(default_factory=dict)

class DeskConfig(BaseModel):
    starting_balance_usd: float = Field(default=280.0, gt=0)
    max_position_fraction: float = Field(default=0.08, gt=0, le=0.08)
    min_analyst_score: float = Field(default=0.60, ge=0, le=1)
    hard_stop_fraction: float = Field(default=0.06, gt=0, lt=1)
    trailing_activation_fraction: float = Field(default=0.12, gt=0, lt=1)
    trailing_drawdown_fraction: float = Field(default=0.06, gt=0, lt=1)
    min_lp_locked_pct: float = Field(default=95.0, ge=0, le=100)
    max_top10_holders_pct: float = Field(default=20.0, ge=0, le=100)
    min_volume_1h_usd: float = Field(default=50000.0, ge=0)
