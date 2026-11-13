"""Independent, fail-closed deterministic Risk Gate evaluator.

No LLM, AI agent, strategy engine, or frontend component may override this service.
"""

from __future__ import annotations

import math
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso


class RiskEvaluationRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: f"REQ-{uuid.uuid4().hex[:10].upper()}")
    venue: str
    arbitrage_type: Literal["intra_venue", "cross_venue"] = "intra_venue"
    strategy_tier: Literal["tier1", "tier2", "tier3"] = "tier1"
    execution_mode: Literal["PAPER_RESEARCH", "PAPER_EXECUTION", "LIVE"] = "PAPER_RESEARCH"
    market_id: str
    requested_notional_usd: float
    current_venue_exposure_usd: float = 0.0
    daily_tier_loss_usd: float = 0.0
    net_edge_pct: float | None
    liquidity_participation_pct: float
    data_age_ms: int
    leg_imbalance_ratio: float = 1.0
    executable: bool = True
    identical_settlement_verified: bool = True
    partial_fill_protection_enabled: bool = True
    ai_suggested_override: bool = False


class RuleCheckResult(BaseModel):
    rule_id: str
    passed: bool
    detail: str


class RiskEvaluationResult(BaseModel):
    audit_id: str
    request_id: str
    approved: bool
    decision: Literal["APPROVED", "DENIED"]
    failed_rules: list[str]
    checks: list[RuleCheckResult]
    kill_switch_active: bool
    summary: str
    decided_at: str


class DeterministicRiskGate:
    """Fail-closed authorization engine backed by immutable RiskPolicy and DuckDB audit trail."""

    def __init__(self, store: DuckDBStore, policy: RiskPolicy | None = None) -> None:
        self.store = store
        self.policy = policy or RiskPolicy()

    def evaluate(self, req: RiskEvaluationRequest) -> RiskEvaluationResult:
        checks: list[RuleCheckResult] = []
        kill_switch_active = self.store.is_kill_switch_active()

        # 1. Kill switch check (absolute priority)
        checks.append(
            RuleCheckResult(
                rule_id="KILL_SWITCH",
                passed=not kill_switch_active,
                detail="Global kill switch is inactive"
                if not kill_switch_active
                else "Global kill switch is ACTIVE — all evaluations halted",
            )
        )

        # 2. Live execution hard lock
        live_allowed = req.execution_mode != "LIVE" or self.policy.live_trading_enabled
        checks.append(
            RuleCheckResult(
                rule_id="LIVE_EXECUTION_LOCK",
                passed=live_allowed,
                detail="Mode is non-live research/paper"
                if live_allowed
                else "LIVE trading is permanently disabled in Phase 1",
            )
        )

        # 3. Tier enablement check (Tier 2 & Tier 3 are inactive future modules)
        tier_enabled = req.strategy_tier in self.policy.enabled_tiers
        checks.append(
            RuleCheckResult(
                rule_id="STRATEGY_TIER_ACTIVE",
                passed=tier_enabled,
                detail=f"Tier {req.strategy_tier} is enabled"
                if tier_enabled
                else f"Tier {req.strategy_tier} is an inactive future module",
            )
        )

        # 4. Venue & arbitrage type check
        venue_norm = req.venue.strip().lower()
        venue_ok = venue_norm in self.policy.enabled_venues and req.arbitrage_type == "intra_venue"
        checks.append(
            RuleCheckResult(
                rule_id="VENUE_AND_ARBITRAGE_SCOPE",
                passed=venue_ok,
                detail="Intra-venue Polymarket scope verified"
                if venue_ok
                else f"Venue '{req.venue}' / type '{req.arbitrage_type}' not authorized in Phase 1",
            )
        )

        # 5. Override immunity check (AI/LLM cannot force approval)
        no_override = not req.ai_suggested_override
        checks.append(
            RuleCheckResult(
                rule_id="NO_AI_OVERRIDE",
                passed=no_override,
                detail="No external/AI override asserted"
                if no_override
                else "Rejected attempt to assert AI/agent override over Risk Gate",
            )
        )

        # 6. Numeric sanity & executability (fail closed on NaN/Inf/None)
        numeric_inputs = [
            req.requested_notional_usd,
            req.current_venue_exposure_usd,
            req.daily_tier_loss_usd,
            req.liquidity_participation_pct,
            req.leg_imbalance_ratio,
        ]
        finite_ok = (
            req.executable
            and req.net_edge_pct is not None
            and math.isfinite(req.net_edge_pct)
            and all(math.isfinite(v) for v in numeric_inputs)
            and req.requested_notional_usd > 0
        )
        checks.append(
            RuleCheckResult(
                rule_id="EXECUTABLE_FINITE_INPUTS",
                passed=finite_ok,
                detail="Executable depth and finite metrics verified"
                if finite_ok
                else "Insufficient executable book depth or non-finite input",
            )
        )

        # 7. Identical settlement verification
        checks.append(
            RuleCheckResult(
                rule_id="IDENTICAL_SETTLEMENT",
                passed=req.identical_settlement_verified,
                detail="Matched binary conditionId settlement verified"
                if req.identical_settlement_verified
                else "Settlement conditions unverified or mismatched",
            )
        )

        # 8. Stale-data rejection
        fresh_ok = 0 <= req.data_age_ms <= self.policy.max_data_age_ms
        checks.append(
            RuleCheckResult(
                rule_id="FRESH_MARKET_DATA",
                passed=fresh_ok,
                detail=f"Data age {req.data_age_ms}ms <= {self.policy.max_data_age_ms}ms"
                if fresh_ok
                else f"Stale or invalid data age ({req.data_age_ms}ms > {self.policy.max_data_age_ms}ms)",
            )
        )

        # 9. Minimum net edge check
        min_edge = (
            self.policy.min_intra_venue_edge_pct
            if req.arbitrage_type == "intra_venue"
            else self.policy.min_cross_venue_edge_pct
        )
        edge_val = req.net_edge_pct if (req.net_edge_pct is not None and math.isfinite(req.net_edge_pct)) else -999.0
        edge_ok = edge_val >= min_edge
        checks.append(
            RuleCheckResult(
                rule_id="MIN_NET_EDGE",
                passed=edge_ok,
                detail=f"Net edge {edge_val:.3f}% >= {min_edge:.2f}%"
                if edge_ok
                else f"Net edge {edge_val:.3f}% below {min_edge:.2f}% minimum",
            )
        )

        # 10. Liquidity participation cap (<= 10%)
        liq_ok = 0.0 < req.liquidity_participation_pct <= self.policy.max_liquidity_participation_pct
        checks.append(
            RuleCheckResult(
                rule_id="LIQUIDITY_PARTICIPATION_CAP",
                passed=liq_ok,
                detail=f"Participation {req.liquidity_participation_pct:.2f}% <= {self.policy.max_liquidity_participation_pct:.1f}%"
                if liq_ok
                else f"Participation {req.liquidity_participation_pct:.2f}% exceeds {self.policy.max_liquidity_participation_pct:.1f}% cap",
            )
        )

        # 11. Venue exposure cap
        venue_cap = self.policy.max_venue_exposure_usd.get(venue_norm, 0.0)
        projected_exposure = req.current_venue_exposure_usd + max(req.requested_notional_usd, 0.0)
        exposure_ok = projected_exposure <= venue_cap and req.current_venue_exposure_usd >= 0.0
        checks.append(
            RuleCheckResult(
                rule_id="MAX_VENUE_EXPOSURE",
                passed=exposure_ok,
                detail=f"Projected exposure ${projected_exposure:.2f} <= ${venue_cap:.2f}"
                if exposure_ok
                else f"Projected exposure ${projected_exposure:.2f} exceeds venue cap ${venue_cap:.2f}",
            )
        )

        # 12. Daily loss limit per tier
        loss_cap = self.policy.daily_loss_limits_usd.get(req.strategy_tier, 0.0)
        loss_ok = 0.0 <= req.daily_tier_loss_usd < loss_cap
        checks.append(
            RuleCheckResult(
                rule_id="DAILY_LOSS_LIMIT",
                passed=loss_ok,
                detail=f"Daily tier loss ${req.daily_tier_loss_usd:.2f} < ${loss_cap:.2f}"
                if loss_ok
                else f"Daily tier loss ${req.daily_tier_loss_usd:.2f} breaches limit ${loss_cap:.2f}",
            )
        )

        # 13. Partial-fill & leg-risk protection
        leg_ok = (
            req.partial_fill_protection_enabled
            and 0.0 < req.leg_imbalance_ratio <= self.policy.max_leg_imbalance_ratio
        )
        checks.append(
            RuleCheckResult(
                rule_id="PARTIAL_FILL_AND_LEG_RISK",
                passed=leg_ok,
                detail=f"Leg protection active; imbalance {req.leg_imbalance_ratio:.2f}x <= {self.policy.max_leg_imbalance_ratio:.1f}x"
                if leg_ok
                else f"Leg-risk guard failed (protection={req.partial_fill_protection_enabled}, imbalance={req.leg_imbalance_ratio:.2f}x)",
            )
        )

        failed_rules = [c.rule_id for c in checks if not c.passed]
        approved = len(failed_rules) == 0
        decision: Literal["APPROVED", "DENIED"] = "APPROVED" if approved else "DENIED"

        if approved:
            summary = f"APPROVED: All {len(checks)} Risk Gate checks passed (net edge {edge_val:.3f}%)"
        else:
            first_failed = next(c for c in checks if not c.passed)
            summary = f"DENIED ({first_failed.rule_id}): {first_failed.detail}"

        audit_id = f"RG-{uuid.uuid4().hex[:12].upper()}"
        decided_at = utc_now_iso()

        self.store.record_risk_audit(
            {
                "audit_id": audit_id,
                "request_id": req.request_id,
                "venue": req.venue,
                "strategy_tier": req.strategy_tier,
                "execution_mode": req.execution_mode,
                "market_id": req.market_id,
                "requested_notional_usd": req.requested_notional_usd
                if math.isfinite(req.requested_notional_usd)
                else 0.0,
                "net_edge_pct": edge_val,
                "liquidity_participation_pct": req.liquidity_participation_pct
                if math.isfinite(req.liquidity_participation_pct)
                else 100.0,
                "data_age_ms": req.data_age_ms,
                "decision": decision,
                "failed_rules": failed_rules,
                "kill_switch_active": kill_switch_active,
                "summary": summary,
                "decided_at": decided_at,
            }
        )

        return RiskEvaluationResult(
            audit_id=audit_id,
            request_id=req.request_id,
            approved=approved,
            decision=decision,
            failed_rules=failed_rules,
            checks=checks,
            kill_switch_active=kill_switch_active,
            summary=summary,
            decided_at=decided_at,
        )
