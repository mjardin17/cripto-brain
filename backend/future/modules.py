"""Inactive future blueprints for Tier 2 (Crypto Delta-Neutral) and Tier 3 (AI Market Research).

Strictly non-executing architectural contracts per Phase 1 specification.
AI outputs in Tier 3 may only reduce position sizes or reject opportunities, never authorize trades.
"""

from __future__ import annotations

from typing import Any


def get_module_registry(counts: dict[str, int] | None = None) -> list[dict[str, Any]]:
    counts = counts or {}
    return [
        {
            "module_id": "risk_gate",
            "name": "Deterministic Risk Gate",
            "tier": "Core Safety",
            "phase": "Phase 1",
            "status": "ACTIVE",
            "execution_capability": "FAIL_CLOSED_AUTHORIZATION",
            "provenance": "LIVE",
            "description": "Independent fail-closed verifier enforcing 1.5% intra-venue edge, 10% depth cap, stale-data rejection, leg-risk bounds, and kill switch.",
            "metrics": {"audits_recorded": counts.get("audits", 0)},
        },
        {
            "module_id": "tier1_polymarket",
            "name": "Tier 1 — Binary Arbitrage Scanner (Polymarket)",
            "tier": "Tier 1",
            "phase": "Phase 1",
            "status": "ACTIVE_READ_ONLY",
            "execution_capability": "READ_ONLY_RESEARCH",
            "provenance": "LIVE",
            "description": "Ingests public Polymarket Gamma markets and CLOB V2 order books, evaluates executable VWAP + taker fees, and logs to DuckDB.",
            "metrics": {
                "snapshots_logged": counts.get("snapshots", 0),
                "opportunities_evaluated": counts.get("opportunities", 0),
            },
        },
        {
            "module_id": "paper_executor",
            "name": "Execution Simulator (Paper Trading)",
            "tier": "Tier 1 Execution",
            "phase": "Phase 2",
            "status": "INACTIVE",
            "execution_capability": "DISABLED_UNTIL_PHASE_2",
            "provenance": "UNAVAILABLE",
            "description": "Limit order, partial fill, latency, slippage, cancel, unwind, and second-leg failure simulator. Scheduled for Phase 2.",
            "metrics": {},
        },
        {
            "module_id": "tier1_kalshi",
            "name": "Cross-Venue Matching (Kalshi V2)",
            "tier": "Tier 1 Cross-Venue",
            "phase": "Phase 3",
            "status": "INACTIVE",
            "execution_capability": "DISABLED_UNTIL_PHASE_3",
            "provenance": "UNAVAILABLE",
            "description": "Read-only Kalshi V2 market ingestion and cross-venue identical-settlement matching (2.0% min edge). Scheduled for Phase 3.",
            "metrics": {},
        },
        {
            "module_id": "tier2_delta_neutral",
            "name": "Tier 2 — Crypto Delta-Neutral & Carry",
            "tier": "Tier 2",
            "phase": "Phase 5",
            "status": "INACTIVE",
            "execution_capability": "DISABLED_FUTURE_MODULE",
            "provenance": "UNAVAILABLE",
            "description": "Funding-rate monitoring, perpetual futures carry, dynamic cointegration, statistical pairs trading, and mean reversion. No live execution.",
            "metrics": {},
        },
        {
            "module_id": "tier3_ai_research",
            "name": "Tier 3 — AI Market Research & Calibration",
            "tier": "Tier 3",
            "phase": "Phase 5",
            "status": "INACTIVE",
            "execution_capability": "DISABLED_FUTURE_MODULE",
            "provenance": "UNAVAILABLE",
            "description": "Multi-agent event analysis, Brier score, log-loss calibration, and confidence-weighted sizing reduction. May only reduce size or reject—never authorize trades.",
            "metrics": {},
        },
    ]
