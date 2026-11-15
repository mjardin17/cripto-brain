"""Stage 3C Tests: Persistent Supervisor, 5 Specialized Agents, Scaling & Blocker Recovery."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ["CERTAINTY_DUCKDB_PATH"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.intelligence.supervisor import SupervisorOrchestrator
from backend.main import create_app
from backend.tests.auth_helpers import authenticate_test_client
from backend.risk_gate.engine import DeterministicRiskGate
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore


def _make_supervisor(db_path: str = ":memory:") -> tuple[DuckDBStore, SupervisorOrchestrator]:
    store = DuckDBStore(db_path=db_path)
    gate = DeterministicRiskGate(store=store, policy=RiskPolicy())
    mm = StructuredMemoryManager(store=store)
    le = LearningEngine(store=store, memory_manager=mm)
    sup = SupervisorOrchestrator(store=store, risk_gate=gate, memory=mm, learning=le)
    return store, sup


def test_task_ownership_strict_fencing_and_duplicate_prevention():
    store, sup = _make_supervisor()
    try:
        sub1 = sup.submit_task(
            task_type="ARBITRAGE_ANALYSIS",
            assigned_agent="ARBITRAGE_RESEARCH",
            payload={"scan_window": "latest"},
        )
        assert sub1["created"] is True
        assert sub1["duplicate_prevented"] is False
        t_id = sub1["task"]["task_id"]

        # Duplicate submission while active must be prevented cleanly
        sub2 = sup.submit_task(
            task_type="ARBITRAGE_ANALYSIS",
            assigned_agent="ARBITRAGE_RESEARCH",
            payload={"scan_window": "latest"},
        )
        assert sub2["created"] is False
        assert sub2["duplicate_prevented"] is True
        assert sub2["task"]["task_id"] == t_id

        w1 = sup.spawn_worker("ARBITRAGE_RESEARCH")
        w2 = sup.spawn_worker("ARBITRAGE_RESEARCH")
        leased = sup.claim_next_task(w1["worker_id"])
        assert leased is not None
        assert leased["worker_id"] == w1["worker_id"]
        token = leased["lease_token"]

        # Wrong worker or wrong fencing token must be rejected
        with pytest.raises(PermissionError, match="Ownership denied"):
            sup.start_task(t_id, worker_id=w2["worker_id"], lease_token=token)
        with pytest.raises(PermissionError, match="Ownership denied"):
            sup.heartbeat(t_id, worker_id=w1["worker_id"], lease_token="WRONG-TOKEN")
        with pytest.raises(PermissionError, match="Ownership denied"):
            sup.complete_task(t_id, worker_id=w2["worker_id"], lease_token=token, result={})

        # Owner completes cleanly; subsequent identical submission creates fresh task
        sup.start_task(t_id, worker_id=w1["worker_id"], lease_token=token)
        done = sup.complete_task(t_id, worker_id=w1["worker_id"], lease_token=token, result={"ok": True})
        assert done["status"] == "COMPLETED"

        sub3 = sup.submit_task(
            task_type="ARBITRAGE_ANALYSIS",
            assigned_agent="ARBITRAGE_RESEARCH",
            payload={"scan_window": "latest"},
        )
        assert sub3["created"] is True
        assert sub3["task"]["task_id"] != t_id
    finally:
        store.close()


def test_task_dependencies_unlock_on_completion_and_fail_closed_on_escalation():
    store, sup = _make_supervisor()
    try:
        parent = sup.submit_task(
            task_type="CHECK_FEED_HEALTH",
            assigned_agent="MARKET_INTELLIGENCE",
            payload={"step": "parent"},
        )["task"]
        child = sup.submit_task(
            task_type="ARBITRAGE_ANALYSIS",
            assigned_agent="ARBITRAGE_RESEARCH",
            payload={"step": "child"},
            depends_on=[parent["task_id"]],
        )["task"]
        assert child["status"] == "PENDING"

        # Worker cannot claim PENDING child before parent completes
        w_arb = sup.spawn_worker("ARBITRAGE_RESEARCH")
        assert sup.claim_next_task(w_arb["worker_id"]) is None

        # Complete parent -> child transitions to QUEUED and becomes claimable
        w_mkt = sup.spawn_worker("MARKET_INTELLIGENCE")
        leased_p = sup.claim_next_task(w_mkt["worker_id"])
        assert leased_p is not None
        sup.start_task(parent["task_id"], worker_id=w_mkt["worker_id"], lease_token=leased_p["lease_token"])
        sup.complete_task(parent["task_id"], worker_id=w_mkt["worker_id"], lease_token=leased_p["lease_token"], result={})

        child_after = sup.get_task(child["task_id"])
        assert child_after is not None and child_after["status"] == "QUEUED"
        assert sup.claim_next_task(w_arb["worker_id"]) is not None

        # Second dependency chain: parent escalates -> child must cascade fail-closed to BLOCKED
        p2 = sup.submit_task(
            task_type="AUDIT_RISK_STATE",
            assigned_agent="RISK_INTELLIGENCE",
            payload={"id": "p2"},
            max_recovery_attempts=0,
        )["task"]
        c2 = sup.submit_task(
            task_type="CONSOLIDATE_LEARNING",
            assigned_agent="LEARNING",
            payload={"id": "c2"},
            depends_on=[p2["task_id"]],
        )["task"]
        w_risk = sup.spawn_worker("RISK_INTELLIGENCE")
        lp2 = sup.claim_next_task(w_risk["worker_id"])
        assert lp2 is not None
        sup.fail_or_recover_task(
            p2["task_id"],
            worker_id=w_risk["worker_id"],
            lease_token=lp2["lease_token"],
            error_message="Unexpected schema error",
        )
        c2_after = sup.get_task(c2["task_id"])
        assert c2_after is not None and c2_after["status"] == "BLOCKED"
    finally:
        store.close()


def test_dynamic_worker_scaling_bounded_by_default_concurrency_4():
    store, sup = _make_supervisor()
    try:
        assert sup.get_policy()["max_concurrency"] == 4

        # Queue 6 tasks -> scale_workers spawns at most 4 workers
        for i in range(6):
            sup.submit_task(
                task_type="CALIBRATE_PROBABILITY",
                assigned_agent="PROBABILITY_RESEARCH",
                payload={"batch": i},
            )
        scale_up = sup.scale_workers()
        assert len(scale_up["spawned"]) == 4
        assert scale_up["active_workers_count"] == 4

        # Attempting to manually spawn a 5th worker fails closed
        with pytest.raises(RuntimeError, match="Concurrency bound reached"):
            sup.spawn_worker("LEARNING")

        # Run full dispatch cycles and verify workers scale down cleanly when queue drains
        r1 = asyncio.run(sup.dispatch_cycle(max_tasks=4))
        assert r1["dispatched_count"] == 4
        assert r1["completed_count"] == 4

        r2 = asyncio.run(sup.dispatch_cycle(max_tasks=4))
        assert r2["dispatched_count"] == 2
        assert r2["completed_count"] == 2
        assert r2["scaling"]["active_workers_count"] == 0
    finally:
        store.close()


def test_expired_lease_heartbeat_failure_and_timeout_recovery():
    store, sup = _make_supervisor()
    try:
        # 1. Expired lease recovery
        t1 = sup.submit_task(
            task_type="CHECK_FEED_HEALTH",
            assigned_agent="MARKET_INTELLIGENCE",
            payload={"case": "lease_expiry"},
        )["task"]
        w1 = sup.spawn_worker("MARKET_INTELLIGENCE")
        leased1 = sup.claim_next_task(w1["worker_id"], lease_duration_seconds=5.0)
        assert leased1 is not None

        future_iso = (datetime.now(timezone.utc) + timedelta(seconds=12)).isoformat()
        reaped1 = sup.reap_stale_tasks(now_iso=future_iso, heartbeat_grace_seconds=60.0)
        assert len(reaped1) == 1
        assert reaped1[0]["blocker"]["blocker_class"] == "WORKER_LEASE_EXPIRED"
        assert reaped1[0]["requeued"] is True
        assert sup.get_worker(w1["worker_id"])["status"] == "UNHEALTHY"  # type: ignore[index]
        assert sup.get_task(t1["task_id"])["status"] == "QUEUED"  # type: ignore[index]

        # 2. Heartbeat extension keeps lease alive; heartbeat loss triggers recovery
        t2 = sup.submit_task(
            task_type="AUDIT_RISK_STATE",
            assigned_agent="RISK_INTELLIGENCE",
            payload={"case": "heartbeat_loss"},
        )["task"]
        w2 = sup.spawn_worker("RISK_INTELLIGENCE")
        leased2 = sup.claim_next_task(w2["worker_id"], lease_duration_seconds=30.0)
        assert leased2 is not None
        sup.start_task(t2["task_id"], worker_id=w2["worker_id"], lease_token=leased2["lease_token"])
        hb = sup.heartbeat(
            t2["task_id"],
            worker_id=w2["worker_id"],
            lease_token=leased2["lease_token"],
            extend_lease_seconds=60.0,
        )
        assert hb["last_heartbeat_at"] is not None

        # Simulate +25s without heartbeat (grace=20s, lease=60s not yet expired)
        hb_stale_iso = (datetime.now(timezone.utc) + timedelta(seconds=25)).isoformat()
        reaped2 = sup.reap_stale_tasks(now_iso=hb_stale_iso, heartbeat_grace_seconds=20.0)
        assert any(e["task"]["task_id"] == t2["task_id"] and e["blocker"]["blocker_class"] == "HEARTBEAT_LOST" for e in reaped2)

        # 3. Runtime asyncio timeout triggers TASK_TIMEOUT recovery
        t3 = sup.submit_task(
            task_type="CUSTOM_SLOW_TASK",
            assigned_agent="ARBITRAGE_RESEARCH",
            payload={"case": "timeout"},
            timeout_seconds=0.1,
        )["task"]

        async def _slow_handler(_task, _payload):
            await asyncio.sleep(0.5)
            return {"ok": True}

        res_to = asyncio.run(
            sup.dispatch_cycle(max_tasks=4, custom_handlers={"CUSTOM_SLOW_TASK": _slow_handler})
        )
        to_outcome = next(o for o in res_to["outcomes"] if o["task_id"] == t3["task_id"])
        assert to_outcome["blocker"] == "TASK_TIMEOUT"
        assert to_outcome["requeued"] is True
    finally:
        store.close()


def test_retry_limits_escalation_and_kill_switch_halt():
    store, sup = _make_supervisor()
    try:
        assert sup.get_policy()["max_recovery_attempts"] == 3
        t = sup.submit_task(
            task_type="CUSTOM_FLAKY",
            assigned_agent="MARKET_INTELLIGENCE",
            payload={"case": "retry_cap"},
            max_retries=3,
            max_recovery_attempts=3,
        )["task"]

        async def _always_transient_error(_task, _payload):
            raise ConnectionError("503 temporary venue outage")

        # Attempts 1, 2, 3 trigger bounded recovery & requeue; attempt 4 exceeds max_recovery_attempts=3 -> ESCALATED
        statuses = []
        for _ in range(4):
            out = asyncio.run(
                sup.dispatch_cycle(
                    max_tasks=1, custom_handlers={"CUSTOM_FLAKY": _always_transient_error}
                )
            )
            assert out["dispatched_count"] == 1
            statuses.append(sup.get_task(t["task_id"])["status"])  # type: ignore[index]

        assert statuses == ["QUEUED", "QUEUED", "QUEUED", "ESCALATED"]
        final_t = sup.get_task(t["task_id"])
        assert final_t is not None
        assert final_t["recovery_attempts"] == 4
        assert "exhausted" in (final_t["escalation_reason"] or "").lower()

        # Kill Switch must immediately halt new task dispatch
        sup.submit_task(
            task_type="AUDIT_RISK_STATE",
            assigned_agent="RISK_INTELLIGENCE",
            payload={"after_kill": True},
        )
        store.set_kill_switch(active=True, reason="Pytest Stage 3C emergency halt", actor="pytest")
        halted = asyncio.run(sup.dispatch_cycle(max_tasks=4))
        assert halted["halted"] is True
        assert halted["reason"] == "KILL_SWITCH_ACTIVE"
        assert halted["dispatched_count"] == 0
        w_probe = None
        with pytest.raises(RuntimeError, match="Kill Switch"):
            sup.spawn_worker("RISK_INTELLIGENCE")
        assert w_probe is None
    finally:
        store.close()


def test_unsafe_recovery_rejection_and_structured_memory_integration(tmp_path):
    db_file = tmp_path / "stage3c_recovery.duckdb"
    store, sup = _make_supervisor(db_path=str(db_file))
    try:
        # Step A: Inject unsafe recovery suggestions -> MUST all be rejected & escalated safely
        t_unsafe = sup.submit_task(
            task_type="ARBITRAGE_ANALYSIS",
            assigned_agent="ARBITRAGE_RESEARCH",
            payload={"target_shares": 25.0},
        )["task"]
        w_arb = sup.spawn_worker("ARBITRAGE_RESEARCH")
        leased = sup.claim_next_task(w_arb["worker_id"])
        assert leased is not None

        res_unsafe = sup.fail_or_recover_task(
            t_unsafe["task_id"],
            worker_id=w_arb["worker_id"],
            lease_token=leased["lease_token"],
            error_message="RISK_GATE denied opportunity below 1.5% edge",
            proposed_strategies=[
                "override_risk_gate",
                "weaken_risk_threshold",
                "bypass_authentication",
                "bypass_rate_limit",
                "execute_live_order",
            ],
        )
        assert res_unsafe["requeued"] is False
        assert res_unsafe["task"]["status"] == "ESCALATED"
        blocker = res_unsafe["blocker"]
        assert blocker["blocker_class"] == "RISK_GATE_DENIED"
        rejected = {
            c["strategy"]: c["rejection_reason"]
            for c in blocker["candidate_strategies"]
            if not c["allowed"]
        }
        for unsafe_name in (
            "override_risk_gate",
            "weaken_risk_threshold",
            "bypass_authentication",
            "bypass_rate_limit",
            "execute_live_order",
        ):
            assert unsafe_name in rejected
            assert rejected[unsafe_name] is not None

        # Step B: HTTP 403 User-Agent blocker searches MEM-PHASE0-001, recovers safely, and records Stage 3B evidence
        t_403 = sup.submit_task(
            task_type="SCAN_POLYMARKET",
            assigned_agent="MARKET_INTELLIGENCE",
            payload={"limit": 5},
        )["task"]
        w_mkt = sup.spawn_worker("MARKET_INTELLIGENCE")
        leased_403 = sup.claim_next_task(w_mkt["worker_id"])
        assert leased_403 is not None

        res_403 = sup.fail_or_recover_task(
            t_403["task_id"],
            worker_id=w_mkt["worker_id"],
            lease_token=leased_403["lease_token"],
            error_message="HTTP 403 from clob.polymarket.com/book using default Python urllib User-Agent",
        )
        assert res_403["requeued"] is True
        b403 = res_403["blocker"]
        assert b403["blocker_class"] == "HTTP_USER_AGENT_BLOCKED"
        assert b403["selected_strategy"] == "use_verified_research_user_agent"
        assert any(h["memory_id"] == "MEM-PHASE0-001" for h in b403["memory_hits"])
        assert b403["learning_cycle_id"].startswith("LCYC-")
        assert sup.get_task(t_403["task_id"])["payload"].get("use_research_user_agent") is True  # type: ignore[index]
    finally:
        store.close()

    # Step C: Persistence across process/DuckDB restart
    store2, sup2 = _make_supervisor(db_path=str(db_file))
    try:
        assert len(sup2.blocker_engine.list_blockers()) == 2
        assert sup2.get_task(t_403["task_id"])["status"] == "QUEUED"  # type: ignore[index]
        assert sup2.get_task(t_unsafe["task_id"])["status"] == "ESCALATED"  # type: ignore[index]
        assert len(sup2.learning.list_cycles()) >= 2
    finally:
        store2.close()


def test_five_agents_and_stage3c_http_endpoints(tmp_path):
    db_file = tmp_path / "stage3c_api.duckdb"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as client:
        authenticate_test_client(client)
        status = client.get("/api/agents/status")
        assert status.status_code == 200
        s_body = status.json()
        assert s_body["live_trading_enabled"] is False
        assert s_body["policy"]["max_concurrency"] == 4
        assert len(s_body["agents"]) == 5

        # Queue one deterministic task per runnable role and dispatch cleanly via HTTP
        tasks_spec = [
            ("CHECK_FEED_HEALTH", "MARKET_INTELLIGENCE"),
            ("ARBITRAGE_ANALYSIS", "ARBITRAGE_RESEARCH"),
            ("CALIBRATE_PROBABILITY", "PROBABILITY_RESEARCH"),
            ("AUDIT_RISK_STATE", "RISK_INTELLIGENCE"),
            ("CONSOLIDATE_LEARNING", "LEARNING"),
        ]
        for tt, role in tasks_spec:
            resp = client.post(
                "/api/agents/tasks",
                json={"task_type": tt, "assigned_agent": role, "payload": {}},
            )
            assert resp.status_code == 200
            assert resp.json()["created"] is True

        d1 = client.post("/api/agents/dispatch", json={"max_tasks": 4}).json()
        assert d1["halted"] is False
        assert d1["dispatched_count"] == 4
        assert d1["completed_count"] == 4

        d2 = client.post("/api/agents/dispatch", json={"max_tasks": 4}).json()
        assert d2["dispatched_count"] == 1
        assert d2["completed_count"] == 1

        all_tasks = client.get("/api/agents/tasks").json()
        assert all_tasks["count"] == 5
        assert all(t["status"] == "COMPLETED" for t in all_tasks["items"])

        # Safety invariants remain intact
        mods = {m["module_id"]: m["status"] for m in client.get("/api/modules/status").json()["modules"]}
        assert mods["tier2_delta_neutral"] == "INACTIVE"
        assert mods["tier3_ai_research"] == "INACTIVE"
        assert client.get("/api/agents/blockers").status_code == 200
