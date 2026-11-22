"""Position sizing and stop management for paper positions only."""
from __future__ import annotations
import math
from .models import DeskConfig, PaperPosition

def calculate_position_notional(balance_usd: float, config: DeskConfig | None = None) -> float:
    cfg = config or DeskConfig()
    if not math.isfinite(balance_usd) or balance_usd <= 0:
        return 0.0
    return round(balance_usd * cfg.max_position_fraction, 2)

def update_position_risk(position: PaperPosition, mark_price: float, config: DeskConfig | None = None) -> tuple[PaperPosition, str | None]:
    """Return updated position and exit reason; no exchange order is submitted."""
    cfg = config or DeskConfig()
    if not math.isfinite(mark_price) or mark_price <= 0:
        return position, "INVALID_OR_MISSING_MARK_PRICE"
    updated = position.model_copy(deep=True)
    updated.mark_price = mark_price
    updated.peak_price = max(updated.peak_price, mark_price)
    pnl_fraction = (mark_price - updated.entry_price) / updated.entry_price
    if pnl_fraction <= -cfg.hard_stop_fraction:
        return updated, "HARD_STOP_6_PERCENT"
    if pnl_fraction >= cfg.trailing_activation_fraction:
        updated.trailing_active = True
    if updated.trailing_active:
        trailing_stop = updated.peak_price * (1.0 - cfg.trailing_drawdown_fraction)
        if mark_price <= trailing_stop:
            return updated, "TRAILING_STOP_6_PERCENT_FROM_PEAK"
    return updated, None
