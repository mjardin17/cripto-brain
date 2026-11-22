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
            "name": "Execution Simulator (Cross-Venue Paper Trading)",
            "tier": "Tier 1 Execution",
            "phase": "Phase 2",
            "status": "ACTIVE_SIMULATED",
            "execution_capability": "SIMULATED_PAPER_ONLY",
            "provenance": "SIMULATED",
            "description": "Deterministic cross-venue paper execution: Risk Gate pre-trade auth, limit IOC orders, partial fills, transit latency, second-leg drift/rejection, and resting bid-book unwinds.",
            "metrics": {
                "paper_orders": counts.get("paper_orders", 0),
                "paper_fills": counts.get("paper_fills", 0),
            },
        },
        {
            "module_id": "tier1_kalshi",
            "name": "Kalshi V2 Read-Only & Cross-Venue Matcher",
            "tier": "Tier 1 Cross-Venue",
            "phase": "Phase 2",
            "status": "ACTIVE_READ_ONLY",
            "execution_capability": "READ_ONLY_RESEARCH",
            "provenance": "LIVE",
            "description": "Public Kalshi Trade API v2 ingestion, complement-symmetry YES/NO book normalization, and fail-closed cross-venue settlement verifier.",
            "metrics": {
                "cross_venue_pairs": counts.get("cross_venue_pairs", 0),
                "cross_venue_opportunities": counts.get("cross_venue_opportunities", 0),
            },
        },
        {
            "module_id": "solana_research_desk",
            "name": "Solana Memecoin Research & Paper Desk",
            "tier": "Tier 1 Solana Research",
            "phase": "Unified Milestone",
            "status": "ACTIVE_SIMULATED",
            "execution_capability": "READ_ONLY_PLUS_PAPER",
            "provenance": "SIMULATED",
            "description": "Six-agent pipeline (SCOUT -> FILTER -> ANALYST -> EXECUTOR -> RISK -> LOGGER) using public DexScreener/GeckoTerminal/Solana RPC/RugCheck/Jupiter read-only evidence, 8% position cap, and -6% hard / +12%->-6% trailing stops.",
            "metrics": {
                "solana_candidates": counts.get("solana_candidates", 0),
                "solana_positions": counts.get("solana_positions", 0),
            },
        },
        {
            "module_id": "ai_councils_learning",
            "name": "Multi-Agent Workforce, Learning Core & Model Ecosystem",
            "tier": "Tier 3 Research",
            "phase": "Stage 3-4A+",
            "status": "ACTIVE_READ_ONLY",
            "execution_capability": "ADVISORY_AND_LEARNING",
            "provenance": "LIVE",
            "description": "Separate Stocks/Crypto AI Councils, 5-role Supervisor workforce, 10-step Learning Core with regression monitoring & rollback, and cost-aware multi-provider routing (local deterministic, Ollama, Groq, OpenRouter, HF, Gemini). Advisory only — never overrides Risk Gate.",
            "metrics": {
                "learning_cycles": counts.get("learning_cycles", 0),
                "memory_entries": counts.get("memory_entries", 0),
            },
        },
        {
            "module_id": "tier2_delta_neutral",
            "name": "Tier 2 — Crypto Delta-Neutral & Carry",
            "tier": "Tier 2",
            "phase": "Future Phase",
            "status": "INACTIVE",
            "execution_capability": "DISABLED_FUTURE_MODULE",
            "provenance": "UNAVAILABLE",
            "description": "Funding-rate monitoring, perpetual futures carry, dynamic cointegration, statistical pairs trading, and mean reversion. No live execution.",
            "metrics": {},
        },
        {
            "module_id": "tier3_ai_research",
            "name": "Tier 3 — Autonomous AI Execution (Disabled Blueprint)",
            "tier": "Tier 3",
            "phase": "Future Phase",
            "status": "INACTIVE",
            "execution_capability": "DISABLED_FUTURE_MODULE",
            "provenance": "UNAVAILABLE",
            "description": "Autonomous AI execution remains permanently INACTIVE. Advisory AI outputs can only reduce size or reject opportunities and never override deterministic safety gates.",
            "metrics": {},
        },
    ]

