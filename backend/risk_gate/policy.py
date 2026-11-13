"""Immutable Risk Gate policy thresholds for Phase 1."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RiskPolicy(BaseModel, frozen=True):
    """Deterministic risk limits enforced fail-closed by the Risk Gate."""

    min_intra_venue_edge_pct: float = Field(
        default=1.50, description="Minimum 1.50% net edge for intra-venue arbitrage"
    )
    min_cross_venue_edge_pct: float = Field(
        default=2.00, description="Minimum 2.00% net edge for cross-venue arbitrage (Phase 3+)"
    )
    max_liquidity_participation_pct: float = Field(
        default=10.0, description="Maximum 10.0% participation in available thinner-leg depth"
    )
    max_venue_exposure_usd: dict[str, float] = Field(
        default_factory=lambda: {"polymarket": 2500.0, "kalshi": 0.0}
    )
    daily_loss_limits_usd: dict[str, float] = Field(
        default_factory=lambda: {"tier1": 250.0, "tier2": 0.0, "tier3": 0.0}
    )
    max_data_age_ms: int = Field(
        default=15_000, description="Reject market data older than 15 seconds"
    )
    max_leg_imbalance_ratio: float = Field(
        default=5.0, description="Maximum ratio between YES and NO top-of-book depths"
    )
    live_trading_enabled: bool = Field(
        default=False, description="Hard lock disabling live order submission in Phase 1"
    )
    enabled_tiers: tuple[str, ...] = ("tier1",)
    enabled_venues: tuple[str, ...] = ("polymarket",)
