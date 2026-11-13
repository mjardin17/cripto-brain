"""FastAPI server for The Certainty Stack (Phase 1 — Risk Gate & Polymarket Tier 1 Research)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from backend.arbitrage.scanner import PolymarketArbitrageScanner
from backend.future.modules import get_module_registry
from backend.risk_gate.engine import (
    DeterministicRiskGate,
    RiskEvaluationRequest,
    RiskEvaluationResult,
)
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi_stub import KalshiVenueStub
from backend.venues.polymarket import PolymarketReadOnlyClient


class ScanRequest(BaseModel):
    limit: int = Field(default=12, ge=1, le=30)
    target_shares: float = Field(default=25.0, gt=0.0, le=5000.0)
    slippage_buffer_bps: float = Field(default=15.0, ge=0.0, le=200.0)


class KillSwitchRequest(BaseModel):
    active: bool
    reason: str = Field(min_length=3, max_length=240)
    actor: str = Field(default="operator", min_length=1, max_length=64)


class MemoryCreateRequest(BaseModel):
    category: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=3, max_length=160)
    summary: str = Field(min_length=5, max_length=1000)
    status: str = Field(default="RECORDED", max_length=48)


def create_app(db_path: str | None = None) -> FastAPI:
    store = DuckDBStore(db_path=db_path)
    policy = RiskPolicy()
    risk_gate = DeterministicRiskGate(store=store, policy=policy)
    poly_client = PolymarketReadOnlyClient()
    scanner = PolymarketArbitrageScanner(store=store, risk_gate=risk_gate, client=poly_client)
    kalshi_stub = KalshiVenueStub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="The Certainty Stack API",
        version="0.1.0-phase1",
        description="Independent fail-closed Risk Gate, Polymarket Gamma/CLOB V2 ingestion, and DuckDB analytics.",
        lifespan=lifespan,
    )

    app.state.store = store
    app.state.risk_gate = risk_gate
    app.state.scanner = scanner
    app.state.poly_client = poly_client

    @app.get("/api/health")
    async def health(probe_venues: bool = Query(default=True)) -> dict[str, Any]:
        counts = store.get_counts()
        kill_switch = store.get_state("kill_switch_active", {"active": False, "reason": "Default inactive"})
        venue_probe = (
            await poly_client.check_connectivity()
            if probe_venues
            else {"gamma_reachable": None, "clob_reachable": None}
        )
        return {
            "service": "the-certainty-stack",
            "phase": "Phase 1 — Risk Gate & Polymarket Tier 1 Research",
            "status": "NOMINAL",
            "live_trading_enabled": False,
            "duckdb": {"connected": True, "path": store.db_path, "counts": counts},
            "risk_gate": {
                "fail_closed": True,
                "kill_switch": kill_switch,
            },
            "venues": {
                "polymarket": {
                    "mode": "READ_ONLY",
                    **venue_probe,
                },
                "kalshi": kalshi_stub.describe(),
            },
            "checked_at": utc_now_iso(),
        }

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:
        counts = store.get_counts()
        kill_switch = store.get_state("kill_switch_active", {"active": False, "reason": "Default inactive"})
        latest_opps = store.list_opportunities(limit=50, latest_scan_only=True)
        all_recent_opps = store.list_opportunities(limit=100, latest_scan_only=False)
        audits = store.list_risk_audit(limit=50)
        memories = store.list_memory(limit=30)
        history_series = store.get_historical_edge_series(limit=40)

        return {
            "phase": "Phase 1",
            "live_trading_enabled": False,
            "portfolio": {
                "provenance": "SIMULATED",
                "paper_capital_allocated_usd": 2500.0,
                "deployed_exposure_usd": 0.0,
                "daily_simulated_pnl_usd": 0.0,
                "paper_execution_status": "UNAVAILABLE (Scheduled for Phase 2)",
                "venue_exposures": [
                    {
                        "venue": "Polymarket",
                        "mode": "READ_ONLY_RESEARCH",
                        "provenance": "LIVE",
                        "current_exposure_usd": 0.0,
                        "max_cap_usd": policy.max_venue_exposure_usd["polymarket"],
                        "daily_loss_limit_usd": policy.daily_loss_limits_usd["tier1"],
                    },
                    {
                        "venue": "Kalshi V2",
                        "mode": "INACTIVE_PHASE_2",
                        "provenance": "UNAVAILABLE",
                        "current_exposure_usd": 0.0,
                        "max_cap_usd": policy.max_venue_exposure_usd["kalshi"],
                        "daily_loss_limit_usd": 0.0,
                    },
                ],
            },
            "risk_gate": {
                "fail_closed": True,
                "policy": policy.model_dump(),
                "kill_switch": kill_switch,
                "total_audits": counts["audits"],
            },
            "modules": get_module_registry(counts),
            "latest_opportunities": latest_opps,
            "historical_opportunities_count": counts["opportunities"],
            "historical_snapshots_count": counts["snapshots"],
            "historical_edge_series": history_series,
            "recent_audits": audits,
            "recent_memories": memories,
            "recent_all_opportunities": all_recent_opps[:30],
            "generated_at": utc_now_iso(),
        }

    @app.post("/api/scan/polymarket")
    async def scan_polymarket(req: ScanRequest) -> dict[str, Any]:
        return await scanner.scan(
            limit=req.limit,
            target_shares=req.target_shares,
            slippage_buffer_bps=req.slippage_buffer_bps,
        )

    @app.get("/api/markets/{market_id}/book")
    async def market_book(market_id: str) -> dict[str, Any]:
        snap = store.get_latest_snapshot_for_market(market_id)
        if not snap:
            raise HTTPException(
                status_code=404,
                detail=f"No persisted DuckDB snapshot found for market_id '{market_id}'. Run a Polymarket scan first.",
            )
        return {
            "provenance": "LIVE_SNAPSHOT",
            "snapshot": snap,
        }

    @app.get("/api/opportunities")
    async def opportunities(
        limit: int = Query(default=100, ge=1, le=500),
        latest_scan_only: bool = Query(default=False),
    ) -> dict[str, Any]:
        items = store.list_opportunities(limit=limit, latest_scan_only=latest_scan_only)
        return {"count": len(items), "items": items}

    @app.post("/api/risk-gate/evaluate", response_model=RiskEvaluationResult)
    async def evaluate_risk_gate(req: RiskEvaluationRequest) -> RiskEvaluationResult:
        return risk_gate.evaluate(req)

    @app.post("/api/risk-gate/kill-switch")
    async def toggle_kill_switch(req: KillSwitchRequest) -> dict[str, Any]:
        state = store.set_kill_switch(active=req.active, reason=req.reason, actor=req.actor)
        store.add_memory(
            category="RISK_GOVERNANCE",
            title=f"Global Kill Switch {'ACTIVATED' if req.active else 'DEACTIVATED'}",
            summary=f"Actor '{req.actor}' set kill_switch={req.active}. Reason: {req.reason}",
            evidence=state,
            status="ENFORCED",
        )
        return state

    @app.get("/api/risk-gate/audit")
    async def risk_audit(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        items = store.list_risk_audit(limit=limit)
        return {"count": len(items), "items": items}

    @app.get("/api/memory")
    async def list_memory(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        items = store.list_memory(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/memory")
    async def create_memory(req: MemoryCreateRequest) -> dict[str, Any]:
        return store.add_memory(
            category=req.category,
            title=req.title,
            summary=req.summary,
            status=req.status,
        )

    @app.get("/api/modules/status")
    async def modules_status() -> dict[str, Any]:
        counts = store.get_counts()
        return {
            "phase": "Phase 1",
            "modules": get_module_registry(counts),
            "kalshi": kalshi_stub.describe(),
        }

    return app


app = create_app(os.environ.get("CERTAINTY_DUCKDB_PATH"))
