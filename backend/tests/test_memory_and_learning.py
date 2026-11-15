"""Unit & persistence tests for Phase 3 Stage 3B Structured Memory & Learning Core."""

from __future__ import annotations

import os

os.environ["CERTAINTY_DUCKDB_PATH"] = ":memory:"

import pytest
from fastapi.testclient import TestClient

from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.main import create_app
from backend.tests.auth_helpers import authenticate_test_client
from backend.storage.duckdb_store import DuckDBStore


def test_at1_persistence_across_restarts_and_honest_phase0_backfill(tmp_path):
    db_file = tmp_path / "stage3b_restart.duckdb"
    store1 = DuckDBStore(db_path=str(db_file))
    # Insert a legacy record with empty evidence alongside seeded MEM-PHASE0-001..003
    empty_leg = store1.add_memory(
        category="RISK_GOVERNANCE",
        title="Unverified claim without evidence",
        summary="Claimed verified but had no evidence payload.",
        evidence={},
        status="VERIFIED_LESSON",
    )
    store1.close()

    # Reopen store: backfill must run deterministically without inventing evidence
    store2 = DuckDBStore(db_path=str(db_file))
    mm2 = StructuredMemoryManager(store2)
    le2 = LearningEngine(store2, mm2)

    p0_1 = mm2.get_entry("MEM-PHASE0-001")
    assert p0_1 is not None
    assert p0_1["status"] == "VERIFIED_LESSON"
    assert p0_1["retention_policy"] == "PERMANENT"
    assert len(p0_1["evidence"]) == 1
    assert len(mm2.get_versions("MEM-PHASE0-001")) == 1

    backfilled_empty = mm2.get_entry(empty_leg["memory_id"])
    assert backfilled_empty is not None
    assert backfilled_empty["evidence"] == []
    # Empty evidence cannot remain VERIFIED_LESSON
    assert backfilled_empty["status"] == "CANDIDATE"

    # Persist new structured entry + learning cycle + regression case, then restart again
    cand = mm2.record_candidate(
        memory_tier="PROCEDURAL",
        category="BLOCKER_RECOVERY",
        title="Retry Polymarket read-only timeout with backoff",
        summary="Bounded 200ms retry recovers transient timeout without bypassing freshness gate.",
        source_agent="research_agent_a",
        evidence=[{"source_type": "HTTP_PROBE", "source_ref_id": "PROBE-101"}],
        confidence=0.82,
    )
    mem_id = cand["entry"]["memory_id"]
    promoted = mm2.verify_and_promote(mem_id, reviewer_id="independent_verifier")
    assert promoted["status"] == "VERIFIED_LESSON"

    cyc = le2.record_recovery_learning(
        blocker_id="BLK-RESTART-1",
        agent_id="recovery_worker",
        subject_key="poly_timeout_retry",
        strategy_name="bounded_backoff_retry",
        succeeded=True,
        input_fixture={"status_code": 504, "attempt": 1},
        expected_invariant={"recovered": True, "live_trading_enabled": False},
        reviewer_id="safety_reviewer",
    )
    assert cyc["stage"] == "VERIFIED_LESSON"
    assert cyc["regression_case_id"] is not None
    store2.close()

    # Third open checks everything survived cleanly across process restarts
    store3 = DuckDBStore(db_path=str(db_file))
    mm3 = StructuredMemoryManager(store3)
    le3 = LearningEngine(store3, mm3)

    persisted_mem = mm3.get_entry(mem_id)
    assert persisted_mem is not None
    assert persisted_mem["status"] == "VERIFIED_LESSON"
    persisted_cyc = le3.get_cycle(cyc["cycle_id"])
    assert persisted_cyc is not None
    assert persisted_cyc["source_ref_id"] == "BLK-RESTART-1"
    reg_cases = le3.list_regression_cases()
    assert any(c["case_id"] == cyc["regression_case_id"] for c in reg_cases)
    store3.close()


def test_at2_deduplication_merges_evidence_and_increments_occurrence():
    store = DuckDBStore(db_path=":memory:")
    mm = StructuredMemoryManager(store)

    r1 = mm.record_candidate(
        memory_tier="SEMANTIC",
        category="VENUE_QUIRK",
        title="Kalshi dollar price fields use 4-decimal strings",
        summary="Kalshi market API returns yes_ask_dollars as string.",
        source_agent="kalshi_adapter",
        explicit_key="semantic:venue_quirk:kalshi-dollar-strings",
        evidence=[{"source_type": "KALSHI_API", "source_ref_id": "TICKER-1"}],
        confidence=0.72,
    )
    assert r1["action"] == "CREATED_CANDIDATE"
    assert r1["deduplicated"] is False
    mem_id = r1["entry"]["memory_id"]

    # Submit near-duplicate / matching key with second piece of evidence
    r2 = mm.record_candidate(
        memory_tier="SEMANTIC",
        category="VENUE_QUIRK",
        title="Kalshi dollar price fields use 4-decimal strings",
        summary="Kalshi market API returns yes_ask_dollars and no_ask_dollars as 4-decimal fixed strings.",
        source_agent="cross_venue_scanner",
        explicit_key="semantic:venue_quirk:kalshi-dollar-strings",
        evidence=[{"source_type": "KALSHI_API", "source_ref_id": "TICKER-2"}],
        confidence=0.75,
    )
    assert r2["action"] == "DEDUPLICATED_MERGE"
    assert r2["deduplicated"] is True
    assert r2["entry"]["memory_id"] == mem_id
    assert r2["entry"]["occurrence_count"] == 2
    assert r2["entry"]["version"] == 2
    assert len(r2["entry"]["evidence"]) == 2
    assert r2["entry"]["confidence"] > 0.75
    store.close()


def test_at3_contradiction_detection_preserves_verified_truth():
    store = DuckDBStore(db_path=":memory:")
    mm = StructuredMemoryManager(store)

    base = mm.record_candidate(
        memory_tier="SEMANTIC",
        category="SETTLEMENT_RULE",
        title="CPI market requires Bureau of Labor Statistics release",
        summary="Resolution source uses official BLS CPI print.",
        source_agent="matcher_agent",
        explicit_key="semantic:settlement:cpi-source",
        content={"claim_key": "cpi_resolution_source", "claim_value": "BLS", "stance": "ALLOW"},
        evidence=[{"source_type": "CONTRACT_SPEC", "source_ref_id": "POLY-CPI-1"}],
        confidence=0.85,
    )["entry"]
    promoted = mm.verify_and_promote(base["memory_id"], reviewer_id="settlement_auditor")
    assert promoted["status"] == "VERIFIED_LESSON"

    # Submit conflicting claim on same claim_key / memory_key
    conflict = mm.record_candidate(
        memory_tier="SEMANTIC",
        category="SETTLEMENT_RULE",
        title="CPI market uses alternative aggregator",
        summary="Resolution source uses private social poll instead.",
        source_agent="noisy_agent",
        explicit_key="semantic:settlement:cpi-source",
        content={"claim_key": "cpi_resolution_source", "claim_value": "SOCIAL_POLL", "stance": "BLOCK"},
        evidence=[{"source_type": "FORUM_NOTE", "source_ref_id": "NOTE-99"}],
        confidence=0.90,
    )
    assert conflict["action"] == "CONTRADICTION_FLAGGED"
    assert conflict["contradiction_detected"] is True
    assert conflict["contradicted_memory_id"] == base["memory_id"]
    assert conflict["entry"]["status"] == "CONTRADICTED"
    assert conflict["entry"]["contradicts_id"] == base["memory_id"]

    # Existing verified truth must remain completely intact
    existing = mm.get_entry(base["memory_id"])
    assert existing is not None
    assert existing["status"] == "VERIFIED_LESSON"
    assert existing["content"]["claim_value"] == "BLS"

    # Cannot promote unresolved CONTRADICTED entry
    with pytest.raises(ValueError, match="CONTRADICTED"):
        mm.verify_and_promote(conflict["entry"]["memory_id"], reviewer_id="settlement_auditor")
    store.close()


def test_at4_evidence_and_anti_self_promotion_gates():
    store = DuckDBStore(db_path=":memory:")
    mm = StructuredMemoryManager(store)
    le = LearningEngine(store, mm)

    no_ev = mm.record_candidate(
        memory_tier="EPISODIC",
        category="EXECUTION_NOTE",
        title="Claimed edge observation without proof",
        summary="Agent claimed edge existed without attaching snapshot or audit ref.",
        source_agent="agent_alpha",
        evidence=[],
        confidence=0.88,
    )["entry"]

    # Rejects self-promotion even before evidence check
    with pytest.raises(PermissionError, match="Self-promotion denied"):
        mm.verify_and_promote(
            no_ev["memory_id"],
            reviewer_id="agent_alpha",
            additional_evidence=[{"source_type": "AUDIT", "source_ref_id": "AUD-1"}],
        )

    # Rejects independent promotion when verifiable evidence is absent
    with pytest.raises(ValueError, match="supporting evidence is required"):
        mm.verify_and_promote(no_ev["memory_id"], reviewer_id="independent_reviewer")

    # Rejects self-promotion in LearningEngine review step and rejects missing-evidence review
    cyc = le.record_observation(
        source_type="OPPORTUNITY",
        source_ref_id="OPP-77",
        agent_id="agent_alpha",
        subject_key="thin_book_slippage",
        hypothesis="Thin depth inflates realized slippage beyond 15 bps",
        expected_outcome={"max_slippage_bps": 15},
        evidence=[],
    )
    le.record_evaluation(cyc["cycle_id"], evaluation_notes="Checked book level 1")
    le.record_decision(cyc["cycle_id"], decision_summary="Simulated fill test")
    le.record_outcome(cyc["cycle_id"], actual_outcome={"max_slippage_bps": 42}, outcome_matched=False)

    with pytest.raises(PermissionError, match="Self-promotion denied"):
        le.review_and_extract_lesson(
            cyc["cycle_id"],
            reviewer_id="agent_alpha",
            review_notes="Self-review attempt",
            approve_lesson=True,
        )

    # New cycle without evidence approved=True must transition cleanly to REJECTED
    cyc2 = le.record_observation(
        source_type="OPPORTUNITY",
        source_ref_id="OPP-78",
        agent_id="agent_alpha",
        subject_key="missing_proof_case",
        hypothesis="Unbacked heuristic",
        expected_outcome={"ok": True},
        evidence=[],
    )
    le.record_evaluation(cyc2["cycle_id"], evaluation_notes="No proof attached")
    le.record_decision(cyc2["cycle_id"], decision_summary="Test unbacked hypothesis")
    le.record_outcome(cyc2["cycle_id"], actual_outcome={"ok": True}, outcome_matched=True)
    reviewed_rej = le.review_and_extract_lesson(
        cyc2["cycle_id"],
        reviewer_id="independent_reviewer",
        review_notes="Attempt promote without proof",
        approve_lesson=True,
    )
    assert reviewed_rej["stage"] == "REJECTED"
    store.close()


def test_at5_version_history_and_recovery():
    store = DuckDBStore(db_path=":memory:")
    mm = StructuredMemoryManager(store)

    created = mm.record_candidate(
        memory_tier="PROCEDURAL",
        category="SCANNER_PARAM",
        title="Recommended target shares for depth check",
        summary="Initial verified target share count is 25 shares.",
        source_agent="scanner_agent",
        content={"target_shares": 25},
        evidence=[{"source_type": "SCAN", "source_ref_id": "SCAN-1"}],
        confidence=0.80,
    )["entry"]
    mem_id = created["memory_id"]

    mm.verify_and_promote(mem_id, reviewer_id="qa_reviewer", review_reason="v2 verified")
    mm.update_entry(
        mem_id,
        changed_by="qa_reviewer",
        change_reason="v3 experimental tweak",
        summary="Changed target shares to 100.",
        content={"target_shares": 100},
        confidence=0.72,
    )

    versions = mm.get_versions(mem_id)
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert versions[1]["status"] == "VERIFIED_LESSON"
    assert versions[2]["content"]["target_shares"] == 100

    # Rollback to verified v2 -> writes v4 preserving full immutable version audit trail
    restored = mm.recover_version(
        mem_id,
        target_version=2,
        changed_by="operator",
        reason="Restore verified v2 target share threshold",
    )
    assert restored["version"] == 4
    assert restored["status"] == "VERIFIED_LESSON"
    assert restored["content"]["target_shares"] == 25
    assert restored["summary"] == "Initial verified target share count is 25 shares."

    all_versions = mm.get_versions(mem_id)
    assert [v["version"] for v in all_versions] == [1, 2, 3, 4]
    store.close()


def test_at6_full_learning_lifecycle_mistakes_and_regression_gate():
    store = DuckDBStore(db_path=":memory:")
    mm = StructuredMemoryManager(store)
    le = LearningEngine(store, mm)

    # Two mismatched outcomes trigger recurring mistake aggregation in AGENT_PERF
    for i in range(2):
        res = le.record_prediction_error_learning(
            prediction_id=f"PRED-{i + 1}",
            agent_id="forecast_agent",
            subject_key="fed_cut_oct",
            predicted_prob=0.90,
            actual_outcome=0,
            reviewer_id="calibration_auditor",
        )
        assert res["stage"] == "VERIFIED_LESSON"
        assert res["source_ref_id"] == f"PRED-{i + 1}"

    mistakes = le.get_recurring_mistakes(min_occurrences=2)
    assert len(mistakes) == 1
    assert mistakes[0]["occurrence_count"] == 2
    assert mistakes[0]["memory_tier"] == "AGENT_PERF"

    # Full 7-stage lifecycle + fail-closed apply guard until regression test passes
    cyc = le.record_observation(
        source_type="EXECUTION",
        source_ref_id="EXEC-SIM-42",
        agent_id="paper_bot",
        subject_key="kalshi_leg2_unwind",
        hypothesis="Using 2-tick buffer prevents orphan leg2 cancellation loss",
        expected_outcome={"unhedged": False},
        evidence=[{"source_type": "PAPER_EXECUTION", "source_ref_id": "EXEC-SIM-42"}],
    )
    le.record_evaluation(cyc["cycle_id"], evaluation_notes="Evaluated historical partial fills")
    le.record_decision(cyc["cycle_id"], decision_summary="Test 2-tick limit buffer in paper sim")
    le.record_outcome(
        cyc["cycle_id"],
        actual_outcome={"unhedged": False, "matched_shares": 25},
        outcome_matched=True,
    )
    reviewed = le.review_and_extract_lesson(
        cyc["cycle_id"],
        reviewer_id="risk_reviewer",
        review_notes="Confirmed against EXEC-SIM-42 fill ledger",
        approve_lesson=True,
        lesson_tier="PROCEDURAL",
        lesson_category="EXECUTION_PLAYBOOK",
        lesson_title="Apply 2-tick limit cushion on leg 2",
        lesson_summary="Verified 2-tick limit buffer avoids second-leg fill misses.",
    )
    assert reviewed["stage"] == "VERIFIED_LESSON"

    # Fail-closed: cannot apply untested lesson to future workflows
    with pytest.raises(ValueError, match="without passing regression"):
        le.apply_verified_lesson(reviewed["cycle_id"], target_workflow="paper_executor")

    reg = le.generate_regression_case(
        source_memory_id=reviewed["promoted_memory_id"],
        source_cycle_id=reviewed["cycle_id"],
        source_ref_id="EXEC-SIM-42",
        category="PAPER_EXECUTION",
        title="Verify leg2 2-tick buffer preserves zero unhedged position",
        description="Leg 2 must remain hedged and live execution disabled.",
        input_fixture={"target_shares": 25, "cushion_ticks": 2},
        expected_invariant={"unhedged": False, "live_trading_enabled": False},
    )
    failing_run = le.run_regression_case(
        reg["case_id"],
        observed_output={"unhedged": True, "live_trading_enabled": False},
    )
    assert failing_run["status"] == "FAILING"
    with pytest.raises(ValueError, match="without passing regression"):
        le.apply_verified_lesson(reviewed["cycle_id"], target_workflow="paper_executor")

    passing_run = le.run_regression_case(
        reg["case_id"],
        observed_output={"unhedged": False, "live_trading_enabled": False},
    )
    assert passing_run["status"] == "PASSING"

    applied = le.apply_verified_lesson(reviewed["cycle_id"], target_workflow="paper_executor")
    assert applied["stage"] == "APPLIED"
    assert applied["applied_flag"] is True
    mem_after = mm.get_entry(reviewed["promoted_memory_id"])
    assert mem_after is not None and mem_after["last_applied_at"] is not None
    store.close()


def test_relevance_retrieval_and_retention_archival():
    store = DuckDBStore(db_path=":memory:")
    mm = StructuredMemoryManager(store)

    # Create a WORKING memory with 1-hour TTL and a VERIFIED PERMANENT memory
    work = mm.record_candidate(
        memory_tier="WORKING",
        category="SCAN_SCRATCH",
        title="Temporary scan observation on Polymarket book",
        summary="Working scratch note for Polymarket CLOB spread.",
        source_agent="scanner",
        evidence=[{"source_type": "SCAN", "source_ref_id": "S-1"}],
        ttl_hours=1.0,
    )["entry"]

    low_cand = mm.record_candidate(
        memory_tier="EPISODIC",
        category="MARKET_NOTE",
        title="Polymarket CLOB V2 book ordering observation",
        summary="Unverified note mentioning CLOB V2 book ordering.",
        source_agent="agent_x",
        confidence=0.55,
    )["entry"]

    # Relevant query must rank verified MEM-PHASE0-002 above low-confidence candidate
    results = mm.search_relevant("Polymarket CLOB V2 book bids asks")
    assert len(results) >= 2
    assert results[0]["memory_id"] == "MEM-PHASE0-002"
    ids = [r["memory_id"] for r in results]
    assert low_cand["memory_id"] in ids
    assert results[0]["relevance_score"] > next(r for r in results if r["memory_id"] == low_cand["memory_id"])["relevance_score"]

    # Simulate +2 hours into future -> WORKING expired and gets archived; MEM-PHASE0-002 stays active
    from datetime import datetime, timedelta, timezone

    future_iso = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    sweep = mm.run_retention_sweep(now_iso=future_iso)
    assert work["memory_id"] in sweep["archived_ids"]
    assert "MEM-PHASE0-002" not in sweep["archived_ids"]

    work_after = mm.get_entry(work["memory_id"])
    assert work_after is not None and work_after["status"] == "ARCHIVED"
    p0_after = mm.get_entry("MEM-PHASE0-002")
    assert p0_after is not None and p0_after["status"] == "VERIFIED_LESSON"
    store.close()


def test_stage3b_api_endpoints_and_safety_invariants(tmp_path):
    db_file = tmp_path / "stage3b_api.duckdb"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as client:
        authenticate_test_client(client)
        # Phase 0 backfill exposed cleanly via structured memory endpoint
        resp = client.get("/api/memory/structured?q=Polymarket")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 3

        # Create structured candidate via HTTP
        post = client.post(
            "/api/memory/structured",
            json={
                "memory_tier": "EPISODIC",
                "category": "SCAN_EVENT",
                "title": "API candidate observation",
                "summary": "Observed normal spread during scan window.",
                "source_agent": "api_tester",
                "evidence": [{"source_type": "HTTP_TEST", "source_ref_id": "T-1"}],
                "confidence": 0.78,
            },
        )
        assert post.status_code == 200
        mem_id = post.json()["entry"]["memory_id"]
        assert post.json()["entry"]["status"] == "CANDIDATE"

        vers = client.get(f"/api/memory/structured/{mem_id}/versions")
        assert vers.status_code == 200
        assert vers.json()["count"] == 1

        # Verify deduplication via API creates v2 and rollback restores v1
        post2 = client.post(
            "/api/memory/structured",
            json={
                "memory_tier": "EPISODIC",
                "category": "SCAN_EVENT",
                "title": "API candidate observation",
                "summary": "Observed tighter spread during second scan window.",
                "source_agent": "api_tester",
                "evidence": [{"source_type": "HTTP_TEST", "source_ref_id": "T-2"}],
                "confidence": 0.80,
            },
        )
        assert post2.status_code == 200
        assert post2.json()["deduplicated"] is True
        assert post2.json()["entry"]["version"] == 2

        rb = client.post(
            f"/api/memory/structured/{mem_id}/rollback",
            json={"target_version": 1, "changed_by": "operator", "reason": "Verify API rollback"},
        )
        assert rb.status_code == 200
        assert rb.json()["restored"]["version"] == 3
        assert rb.json()["restored"]["summary"] == "Observed normal spread during scan window."

        # Learning endpoints return valid shapes
        assert client.get("/api/learning/cycles").status_code == 200
        assert client.get("/api/learning/regression-cases").status_code == 200

        # Permanent safety & compatibility checks preserved
        dash = client.get("/api/dashboard").json()
        assert dash["live_trading_enabled"] is False
        assert dash["risk_gate"]["fail_closed"] is True
        mods = client.get("/api/modules/status").json()["modules"]
        tier_statuses = {m["module_id"]: m["status"] for m in mods}
        assert tier_statuses["tier2_delta_neutral"] == "INACTIVE"
        assert tier_statuses["tier3_ai_research"] == "INACTIVE"
