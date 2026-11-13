"""FastAPI server for The Certainty Stack (Phase 1 — Risk Gate & Polymarket Tier 1 Research)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from backend.arbitrage.cross_venue import CrossVenueScanner
from backend.arbitrage.matcher import assess_contract_equivalence, title_similarity_score
from backend.arbitrage.scanner import PolymarketArbitrageScanner
from backend.execution.paper_simulator import PaperExecutionEngine, PaperExecutionRequest
from backend.future.modules import get_module_registry
from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import MemoryTier, StructuredMemoryManager
from backend.risk_gate.engine import (
    DeterministicRiskGate,
    RiskEvaluationRequest,
    RiskEvaluationResult,
)
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import KalshiReadOnlyClient
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


class CrossVenuePairRegisterRequest(BaseModel):
    pair_id: str | None = Field(default=None, max_length=64)
    poly_market_id: str = Field(min_length=1, max_length=128)
    kalshi_ticker: str = Field(min_length=1, max_length=128)
    resolution_source_verified: bool = False
    predicate_verified: bool = False
    polarity_inverted: bool = False
    void_rules_compatible: bool = False
    allow_early_close_variance: bool = False
    notes: str = Field(default="", max_length=500)


class CrossVenueScanRequest(BaseModel):
    pair_id: str | None = None
    target_shares: float = Field(default=25.0, gt=0.0, le=500.0)
    slippage_buffer_bps: float = Field(default=15.0, ge=0.0, le=200.0)


class StructuredMemoryCreateRequest(BaseModel):
    memory_tier: MemoryTier = "EPISODIC"
    category: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=3, max_length=160)
    summary: str = Field(min_length=5, max_length=1000)
    source_agent: str = Field(default="operator", min_length=2, max_length=64)
    content: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    explicit_key: str | None = Field(default=None, max_length=120)


class MemoryRollbackRequest(BaseModel):
    target_version: int = Field(ge=1)
    changed_by: str = Field(default="operator", min_length=2, max_length=64)
    reason: str = Field(default="Operator version recovery", min_length=3, max_length=240)


def create_app(db_path: str | None = None) -> FastAPI:
    store = DuckDBStore(db_path=db_path)
    policy = RiskPolicy()
    risk_gate = DeterministicRiskGate(store=store, policy=policy)
    poly_client = PolymarketReadOnlyClient()
    kalshi_client = KalshiReadOnlyClient()
    scanner = PolymarketArbitrageScanner(store=store, risk_gate=risk_gate, client=poly_client)
    cross_scanner = CrossVenueScanner(
        store=store, risk_gate=risk_gate, poly_client=poly_client, kalshi_client=kalshi_client
    )
    paper_engine = PaperExecutionEngine(
        store=store, risk_gate=risk_gate, poly_client=poly_client, kalshi_client=kalshi_client
    )
    memory_manager = StructuredMemoryManager(store=store)
    learning_engine = LearningEngine(store=store, memory_manager=memory_manager)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        store.close()

    app = FastAPI(
        title="The Certainty Stack API",
        version="0.3.0-stage3b",
        description="Independent fail-closed Risk Gate, Polymarket + Kalshi read-only ingestion, cross-venue paper simulator, structured memory & learning core, and DuckDB analytics.",
        lifespan=lifespan,
    )

    app.state.store = store
    app.state.risk_gate = risk_gate
    app.state.scanner = scanner
    app.state.cross_scanner = cross_scanner
    app.state.paper_engine = paper_engine
    app.state.memory_manager = memory_manager
    app.state.learning_engine = learning_engine
    app.state.poly_client = poly_client
    app.state.kalshi_client = kalshi_client

    @app.get("/api/health")
    async def health(probe_venues: bool = Query(default=True)) -> dict[str, Any]:
        counts = store.get_counts()
        kill_switch = store.get_state("kill_switch_active", {"active": False, "reason": "Default inactive"})
        venue_probe = (
            await poly_client.check_connectivity()
            if probe_venues
            else {"gamma_reachable": None, "clob_reachable": None}
        )
        kalshi_probe = (
            await kalshi_client.check_connectivity()
            if probe_venues
            else {
                "venue_id": "kalshi",
                "name": "Kalshi Trade API v2",
                "phase": "Phase 2",
                "mode": "READ_ONLY",
                "status": "UNPROBED",
                "provenance": "UNAVAILABLE",
                "reachable": None,
                "live_execution": False,
            }
        )
        return {
            "service": "the-certainty-stack",
            "phase": "Phase 2 — Kalshi Read-Only & Cross-Venue Paper Execution",
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
                "kalshi": kalshi_probe,
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
        paper_summary = store.get_paper_summary()
        venue_exp_map = paper_summary["venue_exposures"]

        return {
            "phase": "Phase 2",
            "live_trading_enabled": False,
            "portfolio": {
                "provenance": "SIMULATED",
                "paper_capital_allocated_usd": 5000.0,
                "deployed_exposure_usd": paper_summary["deployed_exposure_usd"],
                "daily_simulated_pnl_usd": paper_summary["daily_simulated_pnl_usd"],
                "daily_simulated_loss_usd": paper_summary["daily_simulated_loss_usd"],
                "total_realized_pnl_usd": paper_summary["total_realized_pnl_usd"],
                "total_locked_arb_edge_usd": paper_summary["total_locked_arb_edge_usd"],
                "paper_executions_count": paper_summary["executions"],
                "paper_execution_status": "ACTIVE_SIMULATED (Paper Only · No Real Funds)",
                "venue_exposures": [
                    {
                        "venue": "Polymarket",
                        "mode": "READ_ONLY + PAPER_SIM",
                        "provenance": "SIMULATED",
                        "current_exposure_usd": venue_exp_map.get("polymarket", 0.0),
                        "max_cap_usd": policy.max_venue_exposure_usd["polymarket"],
                        "daily_loss_limit_usd": policy.daily_loss_limits_usd["tier1"],
                    },
                    {
                        "venue": "Kalshi V2",
                        "mode": "READ_ONLY + PAPER_SIM",
                        "provenance": "SIMULATED",
                        "current_exposure_usd": venue_exp_map.get("kalshi", 0.0),
                        "max_cap_usd": policy.max_venue_exposure_usd["kalshi"],
                        "daily_loss_limit_usd": policy.daily_loss_limits_usd["tier1"],
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
            "paper_summary": paper_summary,
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

    @app.get("/api/memory/structured")
    async def list_structured_memory(
        q: str = Query(default="", max_length=200),
        tier: str | None = Query(default=None),
        status: str | None = Query(default=None),
        category: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        if q.strip():
            items = memory_manager.search_relevant(
                q.strip(),
                memory_tier=tier.upper() if tier else None,  # type: ignore[arg-type]
                category=category,
                statuses=[status.upper()] if status else None,  # type: ignore[list-item]
                include_archived=include_archived,
                limit=limit,
            )
        else:
            items = memory_manager.list_entries(
                memory_tier=tier,
                status=status,
                category=category,
                include_archived=include_archived,
                limit=limit,
            )
        return {"count": len(items), "items": items}

    @app.post("/api/memory/structured")
    async def create_structured_memory(req: StructuredMemoryCreateRequest) -> dict[str, Any]:
        return memory_manager.record_candidate(
            memory_tier=req.memory_tier,
            category=req.category,
            title=req.title,
            summary=req.summary,
            source_agent=req.source_agent,
            content=req.content,
            evidence=req.evidence,
            tags=req.tags,
            confidence=req.confidence,
            explicit_key=req.explicit_key,
        )

    @app.get("/api/memory/structured/{memory_id}/versions")
    async def get_structured_memory_versions(memory_id: str) -> dict[str, Any]:
        entry = memory_manager.get_entry(memory_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Unknown memory_id '{memory_id}'")
        versions = memory_manager.get_versions(memory_id)
        return {"memory_id": memory_id, "count": len(versions), "versions": versions}

    @app.post("/api/memory/structured/{memory_id}/rollback")
    async def rollback_structured_memory(memory_id: str, req: MemoryRollbackRequest) -> dict[str, Any]:
        try:
            restored = memory_manager.recover_version(
                memory_id,
                target_version=req.target_version,
                changed_by=req.changed_by,
                reason=req.reason,
            )
            return {"restored": restored}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/learning/cycles")
    async def list_learning_cycles(
        stage: str | None = Query(default=None),
        source_type: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        items = learning_engine.list_cycles(stage=stage, source_type=source_type, limit=limit)
        return {"count": len(items), "items": items}

    @app.get("/api/learning/regression-cases")
    async def list_learning_regression_cases(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        items = learning_engine.list_regression_cases(limit=limit)
        return {"count": len(items), "items": items}

    @app.get("/api/kalshi/markets")
    async def kalshi_markets(limit: int = Query(default=15, ge=1, le=40)) -> dict[str, Any]:
        markets = await kalshi_client.fetch_active_binary_markets(limit=limit)
        return {
            "provenance": "LIVE",
            "mode": "READ_ONLY",
            "count": len(markets),
            "markets": [
                {
                    "ticker": m.ticker,
                    "event_ticker": m.event_ticker,
                    "series_ticker": m.series_ticker,
                    "title": m.title,
                    "subtitle": m.subtitle,
                    "yes_sub_title": m.yes_sub_title,
                    "no_sub_title": m.no_sub_title,
                    "close_time": m.close_time,
                    "expiration_time": m.expiration_time,
                    "yes_bid_dollars": m.yes_bid_dollars,
                    "yes_ask_dollars": m.yes_ask_dollars,
                    "no_bid_dollars": m.no_bid_dollars,
                    "no_ask_dollars": m.no_ask_dollars,
                    "volume_24h": m.volume_24h,
                    "open_interest": m.open_interest,
                    "rules_primary": m.rules_primary,
                }
                for m in markets
            ],
        }

    @app.get("/api/kalshi/markets/{ticker}/book")
    async def kalshi_market_book(ticker: str, depth: int = Query(default=15, ge=1, le=50)) -> dict[str, Any]:
        snap = await kalshi_client.fetch_order_book(ticker, depth=depth)
        return {
            "provenance": "LIVE",
            "mode": "READ_ONLY",
            "ticker": snap.ticker,
            "timestamp_ms": snap.timestamp_ms,
            "fetched_at_ms": snap.fetched_at_ms,
            "yes": {
                "best_bid": snap.yes_book.best_bid,
                "best_ask": snap.yes_book.best_ask,
                "bids": [{"price": l.price, "size": l.size} for l in snap.yes_book.bids[:15]],
                "asks": [{"price": l.price, "size": l.size} for l in snap.yes_book.asks[:15]],
                "total_ask_shares": snap.yes_book.total_ask_shares,
            },
            "no": {
                "best_bid": snap.no_book.best_bid,
                "best_ask": snap.no_book.best_ask,
                "bids": [{"price": l.price, "size": l.size} for l in snap.no_book.bids[:15]],
                "asks": [{"price": l.price, "size": l.size} for l in snap.no_book.asks[:15]],
                "total_ask_shares": snap.no_book.total_ask_shares,
            },
        }

    @app.get("/api/cross-venue/pairs")
    async def list_cross_venue_pairs(limit: int = Query(default=100, ge=1, le=200)) -> dict[str, Any]:
        pairs = store.list_cross_venue_pairs(limit=limit)
        return {"count": len(pairs), "items": pairs}

    @app.post("/api/cross-venue/pairs")
    async def register_cross_venue_pair(req: CrossVenuePairRegisterRequest) -> dict[str, Any]:
        try:
            poly_mkt = await app.state.poly_client.fetch_market(req.poly_market_id.strip())
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Polymarket metadata fetch failed: {exc}") from exc
        if poly_mkt is None:
            raise HTTPException(status_code=404, detail=f"Polymarket market '{req.poly_market_id}' not found or non-binary")

        try:
            kalshi_mkt = await app.state.kalshi_client.fetch_market(req.kalshi_ticker.strip())
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Kalshi metadata fetch failed: {exc}") from exc
        if kalshi_mkt is None:
            raise HTTPException(status_code=404, detail=f"Kalshi market '{req.kalshi_ticker}' not found or non-binary")

        assessment = assess_contract_equivalence(
            poly_market=poly_mkt,
            kalshi_market=kalshi_mkt,
            resolution_source_verified=req.resolution_source_verified,
            predicate_verified=req.predicate_verified,
            polarity_inverted=req.polarity_inverted,
            void_rules_compatible=req.void_rules_compatible,
            allow_early_close_variance=req.allow_early_close_variance,
        )
        pair_id = req.pair_id.strip() if req.pair_id else f"XPAIR-{kalshi_mkt.ticker[:14]}-{poly_mkt.market_id[:8]}".upper()
        record = {
            "pair_id": pair_id,
            "poly_market_id": poly_mkt.market_id,
            "poly_question": poly_mkt.question,
            "kalshi_ticker": kalshi_mkt.ticker,
            "kalshi_title": kalshi_mkt.title,
            "resolution_source_verified": req.resolution_source_verified,
            "predicate_verified": req.predicate_verified,
            "polarity_inverted": req.polarity_inverted,
            "void_rules_compatible": req.void_rules_compatible,
            "allow_early_close_variance": req.allow_early_close_variance,
            "identical_settlement_verified": assessment.identical_settlement_verified,
            "settlement_status": assessment.status,
            "mismatch_reasons": list(assessment.mismatch_reasons),
            "cutoff_delta_hours": assessment.cutoff_delta_hours,
            "notes": req.notes,
            "created_at": utc_now_iso(),
        }
        store.upsert_cross_venue_pair(record)
        return {
            "pair": record,
            "title_similarity": title_similarity_score(poly_mkt.question, kalshi_mkt.title),
            "assessment": {
                "identical_settlement_verified": assessment.identical_settlement_verified,
                "status": assessment.status,
                "cutoff_delta_hours": assessment.cutoff_delta_hours,
                "mismatch_reasons": list(assessment.mismatch_reasons),
                "checks": list(assessment.checks),
            },
        }

    @app.get("/api/cross-venue/opportunities")
    async def list_cross_venue_opportunities(limit: int = Query(default=100, ge=1, le=300)) -> dict[str, Any]:
        items = store.list_cross_venue_opportunities(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/cross-venue/scan")
    async def scan_cross_venue(req: CrossVenueScanRequest) -> dict[str, Any]:
        if req.pair_id:
            pair = store.get_cross_venue_pair(req.pair_id)
            if not pair:
                raise HTTPException(status_code=404, detail=f"Unknown cross-venue pair_id '{req.pair_id}'")
            pairs = [pair]
        else:
            pairs = store.list_cross_venue_pairs(limit=25)

        if not pairs:
            return {"count": 0, "results": [], "message": "No cross-venue pairs registered yet."}

        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for pair in pairs:
            try:
                poly_mkt = await app.state.poly_client.fetch_market(pair["poly_market_id"])
                kalshi_mkt = await app.state.kalshi_client.fetch_market(pair["kalshi_ticker"])
                if poly_mkt is None or kalshi_mkt is None:
                    errors.append({"pair_id": pair["pair_id"], "error": "Market inactive or unavailable"})
                    continue
                res = await cross_scanner.evaluate_registered_pair(
                    pair=pair,
                    poly_market=poly_mkt,
                    kalshi_market=kalshi_mkt,
                    target_shares=req.target_shares,
                    slippage_buffer_bps=req.slippage_buffer_bps,
                )
                results.append(
                    {
                        **{k: v for k, v in res.items() if k not in ("best_route", "assessment", "poly_yes_book", "poly_no_book", "kalshi_yes_book", "kalshi_no_book")},
                        "settlement_checks": list(res["assessment"].checks),
                        "mismatch_reasons": list(res["assessment"].mismatch_reasons),
                    }
                )
            except Exception as exc:
                errors.append({"pair_id": pair["pair_id"], "error": str(exc)})

        return {"provenance": "LIVE", "count": len(results), "results": results, "errors": errors}

    @app.post("/api/paper/execute")
    async def execute_paper_trade(req: PaperExecutionRequest) -> dict[str, Any]:
        try:
            return await paper_engine.execute(req)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Paper execution book fetch failed: {exc}") from exc

    @app.get("/api/paper/blotter")
    async def paper_blotter(limit: int = Query(default=100, ge=1, le=300)) -> dict[str, Any]:
        return {
            "provenance": "SIMULATED",
            "live_trading_enabled": False,
            "summary": store.get_paper_summary(),
            "orders": store.list_paper_orders(limit=limit),
            "fills": store.list_paper_fills(limit=limit),
            "positions": store.list_paper_positions(limit=limit),
            "pnl_ledger": store.list_paper_pnl(limit=limit),
        }

    @app.get("/api/modules/status")
    async def modules_status() -> dict[str, Any]:
        counts = store.get_counts()
        return {
            "phase": "Phase 2 — Kalshi Read-Only & Paper Execution",
            "modules": get_module_registry(counts),
            "kalshi": {
                "venue_id": "kalshi",
                "name": "Kalshi Trade API v2",
                "phase": "Phase 2",
                "status": "ACTIVE_READ_ONLY",
                "provenance": "LIVE",
                "live_execution": False,
            },
        }

    return app


app = create_app(os.environ.get("CERTAINTY_DUCKDB_PATH"))
