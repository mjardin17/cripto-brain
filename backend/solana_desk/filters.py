"""Deterministic, fail-closed token safety gate. Never delegate these checks to an LLM."""
from __future__ import annotations
import math
from .models import Candidate, FilterDecision, FilterEvidence, DeskConfig

def evaluate_candidate(candidate: Candidate, evidence: FilterEvidence, config: DeskConfig | None = None) -> FilterDecision:
    cfg = config or DeskConfig()
    failures: list[str] = []
    required_booleans = {
        "mint_authority_revoked": "MINT_AUTHORITY_NOT_VERIFIABLY_REVOKED",
        "freeze_authority_revoked": "FREEZE_AUTHORITY_NOT_VERIFIABLY_REVOKED",
        "sell_simulation_passed": "SELL_SIMULATION_FAILED_OR_UNVERIFIED",
        "dev_wallet_clear": "DEV_WALLET_RISK_PRESENT_OR_UNVERIFIED",
    }
    for field, reason in required_booleans.items():
        if getattr(evidence, field) is not True:
            failures.append(reason)
    numeric_rules = [
        ("lp_locked_pct", cfg.min_lp_locked_pct, "MIN_LP_LOCK_NOT_MET_OR_UNVERIFIED", "min"),
        ("top10_holders_pct", cfg.max_top10_holders_pct, "TOP10_CONCENTRATION_TOO_HIGH_OR_UNVERIFIED", "max"),
        ("volume_1h_usd", cfg.min_volume_1h_usd, "ONE_HOUR_VOLUME_TOO_LOW_OR_UNVERIFIED", "min"),
    ]
    for field, threshold, reason, mode in numeric_rules:
        value = getattr(evidence, field)
        if value is None or not math.isfinite(value):
            failures.append(reason)
        elif (mode == "min" and value < threshold) or (mode == "max" and value > threshold):
            failures.append(reason)
    return FilterDecision(candidate=candidate, passed=not failures, rejection_reasons=failures, evidence=evidence)
