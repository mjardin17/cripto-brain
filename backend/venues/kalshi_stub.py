"""Phase 3 Inactive Stub — Kalshi V2 Read-Only Venue Adapter.

Scheduled for Phase 3 after Phase 1 owner approval and Phase 2 paper execution validation.
"""

from __future__ import annotations

from typing import Any


class KalshiVenueStub:
    venue_id = "kalshi"
    phase = "Phase 3"
    status = "INACTIVE"
    api_base_url = "https://api.elections.kalshi.com/trade-api/v2"
    live_execution = False

    def describe(self) -> dict[str, Any]:
        return {
            "venue_id": self.venue_id,
            "name": "Kalshi Trade API v2",
            "phase": self.phase,
            "status": self.status,
            "provenance": "UNAVAILABLE",
            "live_execution": self.live_execution,
            "notes": "Read-only Kalshi V2 integration and cross-venue semantic matching scheduled for Phase 3.",
        }

    def fetch_markets(self) -> None:
        raise NotImplementedError("Kalshi integration is inactive until Phase 3.")
