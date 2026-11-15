"""Stage 3D Tests: Background Supervisor Loop, Probability Intelligence, Self-Testing & Cost-Aware Model Routing."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

os.environ["CERTAINTY_DUCKDB_PATH"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.storage.duckdb_store import DuckDBStore
from backend.tests.auth_helpers import authenticate_test_client


# ----------------------------------------------------------------------
# PRIORITY ZERO: Lifecycle-Managed Background Supervisor Loop Tests
# ----------------------------------------------------------------------


def test_background_loop_startup_shutdown_duplicate_prevention_and_kill_switch():
    app = create_app(db_path=":memory:", enable_background_loop=False)
    sup = app.state.supervisor
    store = app.state.store

    async def _scenario() -> None:
        # 1. Start loop and verify duplicate start is prevented cleanly
        start1 = await sup.start_background_loop(interval_seconds=0.05, max_tasks_per_tick=2)
        assert start1["started"] is True
        assert start1["duplicate_prevented"] is False
        assert sup.get_background_loop_state()["running"] is True

        dup = await sup.start_background_loop(interval_seconds=0.05, max_tasks_per_tick=2)
        assert dup["started"] is False
        assert dup["duplicate_prevented"] is True
        assert sup.get_background_loop_state()["duplicate_starts_blocked"] >= 1

        # 2. Submit bounded task and confirm background loop automatically dispatches & completes it
        sub = sup.submit_task(
            task_type="AUDIT_RISK_STATE",
            assigned_agent="RISK_INTELLIGENCE",
            payload={"source": "loop_test"},
        )
        t_id = sub["task"]["task_id"]

        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            t_cur = sup.get_task(t_id)
            if t_cur and t_cur["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.05)
        t_final = sup.get_task(t_id)
        assert t_final is not None and t_final["status"] == "COMPLETED"

        # 3. Kill Switch immediately halts background loop dispatch & worker spawning
        store.set_kill_switch(active=True, reason="Stage 3D Kill Switch loop halt test", actor="pytest")
        t_blocked = sup.submit_task(
            task_type="CHECK_FEED_HEALTH",
            assigned_agent="MARKET_INTELLIGENCE",
            payload={"blocked_by": "kill_switch"},
        )["task"]
        await asyncio.sleep(0.20)
        assert sup.get_background_loop_state()["halted_by_kill_switch"] is True
        assert sup.get_task(t_blocked["task_id"])["status"] == "QUEUED"  # type: ignore[index]
        assert sup.list_workers(include_stopped=False) == []

        # 4. Graceful shutdown stops loop cleanly
        stop_res = await sup.stop_background_loop(grace_seconds=1.0)
        assert stop_res["stopped"] is True
        assert sup.get_background_loop_state()["running"] is False

    try:
        asyncio.run(_scenario())
    finally:
        store.close()


def test_background_loop_restart_recovers_expired_leases_and_graceful_shutdown_reclaims_active(tmp_path):
    db_file = str(tmp_path / "stage3d_restart.duckdb")

    # Session 1: Create an active task with an expired lease simulating a prior crash
    app1 = create_app(db_path=db_file, enable_background_loop=False)
    try:
        sup1 = app1.state.supervisor
        t1 = sup1.submit_task(
            task_type="CHECK_FEED_HEALTH",
            assigned_agent="MARKET_INTELLIGENCE",
            payload={"crash": "expired_before_restart"},
        )["task"]
        w1 = sup1.spawn_worker("MARKET_INTELLIGENCE")
        leased = sup1.claim_next_task(w1["worker_id"], lease_duration_seconds=5.0)
        assert leased is not None
        # Force lease_expires_at into the past to simulate process crash
        expired_iso = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        with app1.state.store._lock:
            app1.state.store._conn.execute(
                "UPDATE agent_tasks SET lease_expires_at = ?, last_heartbeat_at = ? WHERE task_id = ?",
                [expired_iso, expired_iso, t1["task_id"]],
            )
    finally:
        app1.state.store.close()

    # Session 2: Starting TestClient (lifespan) must auto-recover expired lease on startup and dispatch cleanly
    app2 = create_app(db_path=db_file, enable_background_loop=True, loop_interval_seconds=0.05)
    with TestClient(app2) as client:
        authenticate_test_client(client)
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            t_cur = app2.state.supervisor.get_task(t1["task_id"])
            if t_cur and t_cur["status"] == "COMPLETED":
                break
            time.sleep(0.05)
        st = client.get("/api/agents/status").json()
        assert st["background_loop"]["startup_recovered_count"] >= 1
        assert app2.state.supervisor.get_task(t1["task_id"])["status"] == "COMPLETED"  # type: ignore[index]

    # Session 3: Verify graceful stop_background_loop reclaims stranded LEASED task safely
    app3 = create_app(db_path=db_file, enable_background_loop=False)
    try:
        sup3 = app3.state.supervisor
        t2 = sup3.submit_task(
            task_type="ARBITRAGE_ANALYSIS",
            assigned_agent="ARBITRAGE_RESEARCH",
            payload={"case": "shutdown_reclaim"},
        )["task"]
        w2 = sup3.spawn_worker("ARBITRAGE_RESEARCH")
        sup3.claim_next_task(w2["worker_id"], lease_duration_seconds=30.0)
        stop_out = asyncio.run(sup3.stop_background_loop())
        assert stop_out["shutdown_recovered_count"] >= 1
        assert sup3.get_task(t2["task_id"])["status"] == "QUEUED"  # type: ignore[index]
        assert sup3.list_workers(include_stopped=False) == []
    finally:
        app3.state.store.close()


# ----------------------------------------------------------------------
# PRIORITY ONE: Probability Intelligence & Sample-Size Calibration Gates
# ----------------------------------------------------------------------


def test_probability_calibration_excludes_open_and_void_and_enforces_min_samples():
    app = create_app(db_path=":memory:", enable_background_loop=False)
    prob = app.state.prob_engine
    learning = app.state.learning_engine
    try:
        # Record 2 OPEN + 1 VOIDED + 2 RESOLVED predictions -> ONLY RESOLVED (2) may count!
        p_open = prob.record_prediction(
            agent_id="probability_research_agent",
            market_id="MKT-OPEN",
            venue="polymarket",
            subject_key="cpi_open",
            question="Open market still trading?",
            predicted_prob=0.90,
        )
        p_void = prob.record_prediction(
            agent_id="probability_research_agent",
            market_id="MKT-VOID",
            venue="polymarket",
            subject_key="cpi_void",
            question="Cancelled event",
            predicted_prob=0.80,
        )
        prob.resolve_prediction(p_void["prediction_id"], outcome="VOID")

        # Known calibration pairs: (0.8 -> outcome=1 -> brier=0.04) and (0.2 -> outcome=0 -> brier=0.04)
        r1 = prob.record_prediction(
            agent_id="probability_research_agent",
            market_id="MKT-1",
            venue="polymarket",
            subject_key="fed_hold",
            question="Fed holds rates?",
            predicted_prob=0.80,
        )
        r2 = prob.record_prediction(
            agent_id="probability_research_agent",
            market_id="MKT-2",
            venue="kalshi",
            subject_key="gdp_miss",
            question="GDP negative?",
            predicted_prob=0.20,
        )
        res1 = prob.resolve_prediction(r1["prediction_id"], outcome=1)
        res2 = prob.resolve_prediction(r2["prediction_id"], outcome=0)
        assert res1["brier_score"] == pytest.approx(0.04, abs=1e-6)
        assert res2["brier_score"] == pytest.approx(0.04, abs=1e-6)

        cal = prob.compute_calibration_summary()
        assert cal["resolved_count"] == 2
        assert cal["open_count"] == 1
        assert cal["voided_count"] == 1
        assert cal["mean_brier_score"] == pytest.approx(0.04, abs=1e-6)
        assert cal["accuracy_pct"] == 100.0
        assert len(cal["buckets"]) == 10

        # Sample threshold < 10: agent must NOT be ranked yet
        cards_before = prob.get_agent_scorecards(min_resolved_samples=10)
        assert cards_before["comparable_agents_count"] == 0
        pr_card = next(c for c in cards_before["scorecards"] if c["agent_id"] == "probability_research_agent")
        assert pr_card["comparison_eligible"] is False
        assert pr_card["rank"] is None
        assert "INSUFFICIENT_SAMPLES" in pr_card["comparison_status"]

        # Add 8 more resolved predictions (including wrong forecasts triggering LearningEngine) -> 10 total
        for idx in range(8):
            wrong_p = 0.85 if idx == 0 else 0.90
            outcome = 0 if idx == 0 else 1
            pr = prob.record_prediction(
                agent_id="probability_research_agent",
                market_id=f"MKT-B{idx}",
                venue="polymarket",
                subject_key=f"batch_evt_{idx}",
                question=f"Batch question {idx}",
                predicted_prob=wrong_p,
            )
            prob.resolve_prediction(pr["prediction_id"], outcome=outcome)

        # Wrong forecast idx=0 (brier = 0.7225 > 0.15) must have created a Stage 3B LearningCycle
        cycles = learning.list_cycles(source_type="PREDICTION")
        assert len(cycles) >= 1

        # Add rival agent with 10 resolved worse predictions so rank order is deterministic
        for idx in range(10):
            pr2 = prob.record_prediction(
                agent_id="arbitrage_research_agent",
                market_id=f"MKT-R{idx}",
                venue="kalshi",
                subject_key=f"rival_evt_{idx}",
                question=f"Rival question {idx}",
                predicted_prob=0.60,
            )
            prob.resolve_prediction(pr2["prediction_id"], outcome=0 if idx < 5 else 1)

        cards_after = prob.get_agent_scorecards(min_resolved_samples=10)
        assert cards_after["comparable_agents_count"] == 2
        ranked = {c["agent_id"]: c for c in cards_after["scorecards"]}
        assert ranked["probability_research_agent"]["comparison_eligible"] is True
        assert ranked["probability_research_agent"]["rank"] == 1
        assert ranked["arbitrage_research_agent"]["rank"] == 2
        assert ranked["probability_research_agent"]["mean_brier_score"] < ranked["arbitrage_research_agent"]["mean_brier_score"]
        assert p_open["status"] == "OPEN"
    finally:
        app.state.store.close()


# ----------------------------------------------------------------------
# PRIORITY TWO: Learning Governance, Anti-Self-Approval, Regression Gate & Rollback
# ----------------------------------------------------------------------


def test_config_governance_blocks_risk_gate_mutation_requires_regression_and_supports_rollback():
    app = create_app(db_path=":memory:", enable_background_loop=False)
    cfg = app.state.config_manager
    learning = app.state.learning_engine
    try:
        # 1. Forbidden security & Risk Gate config domains must fail closed
        for forbidden in ("RISK_GATE", "RISK_POLICY", "SECURITY_PERMISSIONS", "LIVE_TRADING"):
            with pytest.raises(PermissionError, match="Forbidden config_domain"):
                cfg.propose_config_update(
                    config_domain=forbidden,
                    proposed_config={"min_intra_venue_edge_pct": 0.0},
                    proposed_by="arbitrage_research_agent",
                    change_reason="Attempted threshold relaxation",
                )

        # Enabling live trading or AI override in an allowed domain also fails closed
        with pytest.raises(PermissionError, match="Safety lock"):
            cfg.propose_config_update(
                config_domain="AGENT_PIPELINE",
                proposed_config={"live_trading_enabled": True},
                proposed_by="learning_agent",
                change_reason="Sneaky live trading flag",
            )

        # 2. Propose without regression test -> activation denied
        cand_no_reg = cfg.propose_config_update(
            config_domain="AGENT_PIPELINE",
            proposed_config={"default_max_concurrency": 3, "live_trading_enabled": False},
            proposed_by="learning_agent",
            change_reason="Lower concurrency for rate limit stability",
        )
        with pytest.raises(ValueError, match="Regression test case is required"):
            cfg.activate_config_version(cand_no_reg["config_version_id"], reviewed_by="risk_intelligence_agent")

        # 3. Generate a regression case that is NOT YET PASSING -> still fails closed
        reg_case = learning.generate_regression_case(
            source_ref_id="PRED-ERR-1",
            category="CALIBRATION_TUNING",
            title="Ensure bounded concurrency <= 4",
            description="Verify pipeline config stays bounded",
            input_fixture={"proposed_concurrency": 3},
            expected_invariant={"bounded": True},
        )
        cand_untested = cfg.propose_config_update(
            config_domain="AGENT_PIPELINE",
            proposed_config={"default_max_concurrency": 3, "live_trading_enabled": False},
            proposed_by="learning_agent",
            change_reason="Concurrency 3 with unverified test",
            regression_case_id=reg_case["case_id"],
        )
        with pytest.raises(ValueError, match="not PASSING"):
            cfg.activate_config_version(cand_untested["config_version_id"], reviewed_by="risk_intelligence_agent")

        # 4. Self-activation denied even when regression test passes
        passed = learning.run_regression_case(reg_case["case_id"], observed_output={"bounded": True})
        assert passed["status"] == "PASSING"
        with pytest.raises(PermissionError, match="Self-activation denied"):
            cfg.activate_config_version(cand_untested["config_version_id"], reviewed_by="learning_agent")

        # 5. Independent review + PASSING regression test activates cleanly, and rollback restores v1
        activated = cfg.activate_config_version(
            cand_untested["config_version_id"], reviewed_by="risk_intelligence_agent"
        )
        assert activated["status"] == "ACTIVE"
        assert activated["regression_passed"] is True
        assert activated["config"]["default_max_concurrency"] == 3

        rolled = cfg.rollback_config(
            config_domain="AGENT_PIPELINE",
            target_version=1,
            actor="operator",
            reason="Restore baseline concurrency=4",
        )
        assert rolled["status"] == "ACTIVE"
        assert rolled["version"] > activated["version"]
        assert rolled["config"]["default_max_concurrency"] == 4
    finally:
        app.state.store.close()


# ----------------------------------------------------------------------
# PRIORITY THREE: Cost-Aware Model Routing, Budget Enforcement & Zero Key Leak
# ----------------------------------------------------------------------


def test_cost_aware_model_router_deterministic_first_caps_and_no_key_leak(monkeypatch):
    app = create_app(db_path=":memory:", enable_background_loop=False)
    router = app.state.model_router
    try:
        # 1. No API key configured: deterministic tasks run at $0.00; AI request falls back with $0.00
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        d1 = router.route_task(
            task_id="T-DET",
            agent_id="arbitrage_research_agent",
            task_category="ARBITRAGE_MATH",
            prefer_ai=True,
        )
        assert d1["selected_tier"] == "DETERMINISTIC"
        assert d1["actual_cost_usd"] == 0.0

        d_nokey = router.route_task(
            task_id="T-NOKEY",
            agent_id="market_intelligence_agent",
            task_category="RESEARCH_SYNTHESIS",
            prefer_ai=True,
        )
        assert d_nokey["selected_tier"] == "UNAVAILABLE"
        assert d_nokey["actual_cost_usd"] == 0.0
        assert d_nokey["api_key_configured"] is False

        # 2. Configure fake server key -> NEVER leak secret string; enforce $0.01/task & $0.50/day caps
        secret_sentinel = "AIzaSy-TEST-DO-NOT-EXPOSE-TO-FRONTEND-999"
        monkeypatch.setenv("GEMINI_API_KEY", secret_sentinel)

        d_ok = router.route_task(
            task_id="T-AI-OK",
            agent_id="learning_agent",
            task_category="RESEARCH_SYNTHESIS",
            prefer_ai=True,
            estimated_input_tokens=1000,
            estimated_output_tokens=500,
        )
        assert d_ok["selected_tier"] == "ECONOMICAL_AI"
        assert 0.0 < d_ok["actual_cost_usd"] <= 0.01

        # Exceeds per-task $0.01 cap -> BLOCKED with $0.00 spent
        d_over_task = router.route_task(
            task_id="T-OVER-TASK",
            agent_id="learning_agent",
            task_category="RESEARCH_SYNTHESIS",
            prefer_ai=True,
            estimated_cost_override_usd=0.025,
        )
        assert d_over_task["selected_tier"] == "BLOCKED_BUDGET"
        assert d_over_task["actual_cost_usd"] == 0.0

        # Exhaust $0.50 daily cap in bounded increments -> 51st $0.01 task must be BLOCKED
        for _ in range(50):
            router.route_task(
                task_id=None,
                agent_id="learning_agent",
                task_category="RESEARCH_SYNTHESIS",
                prefer_ai=True,
                estimated_cost_override_usd=0.01,
            )
        d_over_daily = router.route_task(
            task_id="T-OVER-DAY",
            agent_id="learning_agent",
            task_category="RESEARCH_SYNTHESIS",
            prefer_ai=True,
            estimated_cost_override_usd=0.01,
        )
        assert d_over_daily["selected_tier"] == "BLOCKED_BUDGET"
        assert d_over_daily["actual_cost_usd"] == 0.0

        summary = router.get_usage_summary()
        assert summary["api_key_configured"] is True
        assert summary["api_key_exposed_to_frontend"] is False
        assert secret_sentinel not in json.dumps(summary)
    finally:
        app.state.store.close()


# ----------------------------------------------------------------------
# STAGE 3D ENDPOINTS & COMMAND CENTER API INTEGRATION
# ----------------------------------------------------------------------


def test_stage3d_http_endpoints_and_safety_locks(tmp_path, monkeypatch):
    secret_sentinel = "SECRET-KEY-NEVER-EXPOSED-XYZ"
    monkeypatch.setenv("GEMINI_API_KEY", secret_sentinel)
    db_file = str(tmp_path / "stage3d_http.duckdb")
    app = create_app(db_path=db_file, enable_background_loop=True, loop_interval_seconds=5.0)

    with TestClient(app) as client:
        authenticate_test_client(client)
        # Record and resolve prediction over HTTP
        p_resp = client.post(
            "/api/intelligence/predictions",
            json={
                "agent_id": "probability_research_agent",
                "market_id": "POLY-123",
                "venue": "polymarket",
                "subject_key": "cpi_print",
                "question": "Will CPI print <= 3.0%?",
                "predicted_prob": 0.72,
                "market_implied_prob": 0.65,
                "confidence": 0.78,
                "rationale": "Deterministic midpoint & dispersion check",
                "provenance": "SIMULATED",
            },
        )
        assert p_resp.status_code == 200
        pred_id = p_resp.json()["prediction_id"]

        res_resp = client.post(
            f"/api/intelligence/predictions/{pred_id}/resolve",
            json={"outcome": 0, "resolution_source": "bls_release"},
        )
        assert res_resp.status_code == 200
        resolved = res_resp.json()
        assert resolved["status"] == "RESOLVED"
        assert resolved["brier_score"] == pytest.approx(0.5184, abs=1e-6)
        assert resolved["learning_cycle_id"].startswith("LCYC-")

        # Calibration, Scorecards, Model Routing & Configs
        assert client.get("/api/intelligence/calibration").json()["resolved_count"] == 1
        assert len(client.get("/api/intelligence/scorecards").json()["scorecards"]) >= 1

        route_resp = client.post(
            "/api/intelligence/model-routing/route",
            json={
                "agent_id": "probability_research_agent",
                "task_category": "CALIBRATION_STATS",
                "prefer_ai": True,
            },
        )
        assert route_resp.status_code == 200
        assert route_resp.json()["selected_tier"] == "DETERMINISTIC"

        forb_cfg = client.post(
            "/api/intelligence/configs/propose",
            json={
                "config_domain": "RISK_GATE",
                "proposed_config": {"min_intra_venue_edge_pct": 0.1},
                "proposed_by": "probability_research_agent",
                "change_reason": "Unsafe threshold relaxation",
            },
        )
        assert forb_cfg.status_code == 403

        # Consolidated Command Center payload
        cc = client.get("/api/intelligence/command-center")
        assert cc.status_code == 200
        body = cc.json()
        assert body["live_trading_enabled"] is False
        assert body["supervisor"]["background_loop"]["running"] is True
        assert secret_sentinel not in cc.text

        # Loop stop & start HTTP controls
        stop_r = client.post("/api/agents/loop/stop")
        assert stop_r.status_code == 200 and stop_r.json()["stopped"] is True
        start_r = client.post(
            "/api/agents/loop/start", json={"interval_seconds": 5.0, "max_tasks_per_tick": 4}
        )
        assert start_r.status_code == 200 and start_r.json()["started"] is True
