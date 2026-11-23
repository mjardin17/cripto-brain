"""Correctness Audit & Arena-to-Cripto-Brain Forecast Ingestion Tests.

Verifies:
1. YES/NO outcomes evaluated strictly against the linked forecast's binary YES
   proposition (never trade buy/sell direction).
2. VOID outcomes never receive binary Brier/log-loss scores and never train
   Learning Core mistake memory.
3. Fail-closed validation of linked forecasts (wrong market, wrong/expired
   timeframe, future timestamp, inverted polarity, conflicting/unlinked manual
   Arena model, duplicate resolution, double blotter linkage).
4. True atomic rollback & clean retry across every stage of DuckDB,
   LearningEngine, and ProbabilityIntelligenceEngine (zero partial updates or
   duplicate learning cycles).
5. Full settlement evidence, provenance, timestamps, and confirming operator
   retention on both paper_pnl_ledger and predictions.
6. Arena-to-Cripto-Brain forecast ingestion into the unified predictions table
   (single + atomic batch, dedup, direction/probability validation, leaderboard,
   RBAC, and end-to-end blotter confirmation loop).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.storage.duckdb_store import utc_now_iso
from backend.tests.auth_helpers import authenticate_test_client
from backend.tests.test_paper_simulator import _StubKalshiClient, _StubPolyClient


def _setup_app_with_verified_pair(
    *,
    pair_id: str = "XPAIR-AUDIT-01",
    poly_market_id: str = "POLY-CPI-26",
    kalshi_ticker: str = "KXCPI-26DEC-T30",
    polarity_inverted: bool = False,
):
    app = create_app(db_path=":memory:", enable_background_loop=False)
    app.state.poly_client = _StubPolyClient()
    app.state.kalshi_client = _StubKalshiClient()
    app.state.cross_scanner.poly_client = app.state.poly_client
    app.state.cross_scanner.kalshi_client = app.state.kalshi_client
    app.state.paper_engine.poly_client = app.state.poly_client
    app.state.paper_engine.kalshi_client = app.state.kalshi_client

    app.state.store.upsert_cross_venue_pair(
        {
            "pair_id": pair_id,
            "poly_market_id": poly_market_id,
            "poly_question": "Will US CPI exceed 3.0% in Dec 2026?",
            "kalshi_ticker": kalshi_ticker,
            "kalshi_title": "Will CPI exceed 3.0% in December 2026?",
            "resolution_source_verified": True,
            "predicate_verified": True,
            "polarity_inverted": polarity_inverted,
            "void_rules_compatible": True,
            "allow_early_close_variance": False,
            "identical_settlement_verified": not polarity_inverted,
            "settlement_status": "MISMATCH_BLOCKED" if polarity_inverted else "VERIFIED",
            "mismatch_reasons": ["POLARITY_INVERTED"] if polarity_inverted else [],
            "cutoff_delta_hours": 0.5,
            "notes": "Audit test pair",
            "created_at": utc_now_iso(),
        }
    )
    return app


def _seed_paper_ledger(
    app,
    *,
    ledger_id: str = "PPNL-AUDIT-01",
    execution_id: str = "PEXEC-AUDIT-01",
    pair_id: str = "XPAIR-AUDIT-01",
    recorded_at: str | None = None,
) -> dict:
    return app.state.store.record_paper_pnl(
        {
            "ledger_id": ledger_id,
            "execution_id": execution_id,
            "pair_id": pair_id,
            "event_type": "HEDGED_ENTRY",
            "outcome_status": "HEDGED_FULL_FILL",
            "matched_shares": 25.0,
            "unwound_shares": 0.0,
            "gross_cost_usd": 23.5,
            "fees_usd": 0.4,
            "realized_pnl_usd": 0.0,
            "locked_arb_edge_usd": 1.1,
            "notes": "Seeded audit ledger row",
            "provenance": "SIMULATED",
            "recorded_at": recorded_at or utc_now_iso(),
        }
    )


def _arena_payload(**overrides) -> dict:
    past_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    base = {
        "model_id": "gemini-2.5-pro",
        "provider": "gemini",
        "task_category": "prediction_market_binary",
        "prompt_version": "prompt-v2.1",
        "benchmark_version": "bench-2026.11",
        "market_id": "POLY-CPI-26",
        "venue": "polymarket",
        "timeframe": "2026-12-31T23:59:59Z",
        "forecast_timestamp": past_ts,
        "dataset_snapshot_id": "SNAP-CPI-20261120",
        "predicted_prob": 0.80,
        "predicted_direction": "YES",
        "question": "Will US CPI exceed 3.0% in Dec 2026?",
        "market_implied_prob": 0.54,
        "confidence": 0.82,
        "rationale": "Arena benchmark ensemble forecast.",
        "provenance": "LIVE",
        "external_forecast_id": None,
    }
    base.update(overrides)
    return base


def test_yes_no_evaluated_against_forecast_proposition_not_trade_direction():
    app = _setup_app_with_verified_pair()
    with TestClient(app) as client:
        authenticate_test_client(client)

        # Execute two hedged paper trades with opposite venue leg directions
        e1 = client.post(
            "/api/paper/execute",
            json={
                "pair_id": "XPAIR-AUDIT-01",
                "direction": "POLY_YES_KALSHI_NO",
                "target_shares": 25.0,
                "scenario": "LIVE_BOOKS",
            },
        )
        assert e1.status_code == 200, e1.text
        ledger1_id = e1.json()["pnl_entry"]["ledger_id"]

        e2 = client.post(
            "/api/paper/execute",
            json={
                "pair_id": "XPAIR-AUDIT-01",
                "direction": "POLY_YES_KALSHI_NO",
                "target_shares": 15.0,
                "scenario": "LIVE_BOOKS",
            },
        )
        assert e2.status_code == 200, e2.text
        ledger2_id = e2.json()["pnl_entry"]["ledger_id"]

        # Forecast 1 predicts NO (p_yes = 0.25) on POLY-CPI-26 even though paper leg bought POLY YES
        f1 = client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(
                predicted_prob=0.25,
                predicted_direction="NO",
                external_forecast_id="ARENA-PROP-NO-1",
            ),
        )
        assert f1.status_code == 200, f1.text
        pred1_id = f1.json()["forecast"]["prediction_id"]

        # Confirm NO -> matches forecast's NO prediction (actual_outcome=0, Brier=(0.25-0)^2=0.0625)
        r1 = client.post(
            f"/api/paper/blotter/{ledger1_id}/resolve",
            json={
                "resolution_result": "NO",
                "resolution_source": "bls_official_cpi_release",
                "notes": "CPI printed 2.8%, below 3.0% threshold.",
                "linked_prediction_id": pred1_id,
            },
        )
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        assert d1["resolved_prediction"]["actual_outcome"] == 0
        assert d1["resolved_prediction"]["brier_score"] == pytest.approx(0.0625, abs=1e-4)
        assert d1["learning_cycle"]["outcome_matched"] is True
        assert d1["learning_cycle"]["expected_outcome"]["outcome_definition"] == "FORECAST_YES_PROPOSITION"
        # Authoritative attribution derived strictly from linked forecast without manual input
        assert d1["entry"]["arena_model_id"] == "arena:gemini/gemini-2.5-pro"
        assert d1["learning_cycle"]["agent_id"] == "arena:gemini/gemini-2.5-pro"

        # Forecast 2 predicts YES (p_yes = 0.80) on KXCPI-26DEC-T30 (Kalshi side of same pair)
        f2 = client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(
                market_id="KXCPI-26DEC-T30",
                venue="kalshi",
                predicted_prob=0.80,
                predicted_direction="YES",
                external_forecast_id="ARENA-PROP-YES-2",
            ),
        )
        assert f2.status_code == 200, f2.text
        pred2_id = f2.json()["forecast"]["prediction_id"]

        # Confirm NO -> forecast was WRONG (actual_outcome=0, Brier=(0.80-0)^2=0.64, mistake logged)
        r2 = client.post(
            f"/api/paper/blotter/{ledger2_id}/resolve",
            json={
                "resolution_result": "NO",
                "resolution_source": "kalshi_official_settlement",
                "notes": "Kalshi contract settled NO.",
                "linked_prediction_id": pred2_id,
            },
        )
        assert r2.status_code == 200, r2.text
        d2 = r2.json()
        assert d2["resolved_prediction"]["actual_outcome"] == 0
        assert d2["resolved_prediction"]["brier_score"] == pytest.approx(0.64, abs=1e-4)
        assert d2["learning_cycle"]["outcome_matched"] is False
        mistakes = app.state.memory_manager.list_entries(
            memory_tier="AGENT_PERF", category="RECURRING_MISTAKE", include_archived=True
        )
        assert len(mistakes) == 1


def test_void_outcome_never_scored_never_trains_mistakes_and_retains_evidence():
    app = _setup_app_with_verified_pair()
    _seed_paper_ledger(app, ledger_id="PPNL-VOID-01")
    with TestClient(app) as client:
        authenticate_test_client(client)

        ing = client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(
                predicted_prob=0.90,
                predicted_direction="YES",
                external_forecast_id="ARENA-VOID-1",
            ),
        )
        assert ing.status_code == 200, ing.text
        pred_id = ing.json()["forecast"]["prediction_id"]

        res = client.post(
            "/api/paper/blotter/PPNL-VOID-01/resolve",
            json={
                "resolution_result": "VOID",
                "resolution_source": "polymarket_uma_void_ruling",
                "notes": "Contract cancelled due to ambiguous oracle wording.",
                "linked_prediction_id": pred_id,
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()

        # Prediction must be VOIDED with NULL binary scores & retained VOID evidence
        rp = data["resolved_prediction"]
        assert rp["status"] == "VOIDED"
        assert rp["actual_outcome"] is None
        assert rp["brier_score"] is None
        assert rp["log_loss"] is None
        assert rp["resolved_at"] is not None
        void_evs = [
            ev for ev in rp["evidence"] if ev.get("source_type") == "CONFIRMED_VOID_RESOLUTION"
        ]
        assert len(void_evs) == 1
        assert void_evs[0]["confirmed_by"] == "testowner"
        assert void_evs[0]["source_ref_id"] == "polymarket_uma_void_ruling"
        assert void_evs[0]["provenance"] == "OPERATOR_CONFIRMED"
        assert void_evs[0]["scored"] is False

        # Learning Core must NOT treat VOID as incorrect or record mistake memory
        assert data["learning_cycle"]["outcome_matched"] is None
        assert data["learning_cycle"]["actual_outcome"]["void_excluded_from_training"] is True
        mistakes = app.state.memory_manager.list_entries(
            memory_tier="AGENT_PERF", category="RECURRING_MISTAKE", include_archived=True
        )
        assert mistakes == []

        # Ledger entry retains full settlement provenance & evidence
        entry = data["entry"]
        assert entry["resolution_result"] == "VOID"
        assert entry["resolution_source"] == "polymarket_uma_void_ruling"
        assert entry["resolved_by"] == "testowner"
        assert entry["resolved_at"] is not None
        assert len(entry["resolution_evidence"]) == 1
        assert entry["resolution_evidence"][0]["provenance"] == "OPERATOR_CONFIRMED"
        assert entry["resolution_evidence"][0]["scored"] is False

        # Calibration summary & Arena leaderboard must NOT include VOID in Brier/log-loss
        cal = client.get("/api/intelligence/calibration").json()
        assert cal["resolved_count"] == 0
        assert cal["voided_count"] == 1
        assert cal["mean_brier_score"] is None
        assert cal["mean_log_loss"] is None

        arena_list = client.get("/api/intelligence/arena/forecasts").json()
        lb_model = arena_list["leaderboard"]["models"][0]
        assert lb_model["resolved_count"] == 0
        assert lb_model["voided_count"] == 1
        assert lb_model["mean_brier_score"] is None


def test_incorrect_forecast_linkage_and_duplicate_resolution_rejected():
    app = _setup_app_with_verified_pair()
    _seed_paper_ledger(app, ledger_id="PPNL-LINK-1", execution_id="PEXEC-LINK-1")
    _seed_paper_ledger(app, ledger_id="PPNL-LINK-2", execution_id="PEXEC-LINK-2")

    # Also register an inverted-polarity pair + ledger row
    app.state.store.upsert_cross_venue_pair(
        {
            "pair_id": "XPAIR-INVERTED",
            "poly_market_id": "POLY-CPI-26",
            "poly_question": "Inverted pair test",
            "kalshi_ticker": "KXCPI-26DEC-T30",
            "kalshi_title": "Inverted pair test",
            "resolution_source_verified": True,
            "predicate_verified": True,
            "polarity_inverted": True,
            "void_rules_compatible": True,
            "allow_early_close_variance": False,
            "identical_settlement_verified": False,
            "settlement_status": "MISMATCH_BLOCKED",
            "mismatch_reasons": ["POLARITY_INVERTED"],
            "cutoff_delta_hours": 0.5,
            "notes": "",
            "created_at": utc_now_iso(),
        }
    )
    _seed_paper_ledger(
        app,
        ledger_id="PPNL-INVERTED-1",
        execution_id="PEXEC-INV-1",
        pair_id="XPAIR-INVERTED",
    )

    with TestClient(app) as client:
        authenticate_test_client(client)

        # 1) Manual Arena model attribution without linked forecast -> 400
        no_link = client.post(
            "/api/paper/blotter/PPNL-LINK-1/resolve",
            json={
                "resolution_result": "YES",
                "arena_model_id": "arena:openai/gpt-5",
            },
        )
        assert no_link.status_code == 400
        assert "requires a linked forecast" in no_link.json()["detail"].lower()

        # 2) Wrong market forecast -> 409
        wrong_mkt = client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(market_id="POLY-UNRELATED-999", external_forecast_id="ARENA-WMKT"),
        ).json()["forecast"]
        res_wmkt = client.post(
            "/api/paper/blotter/PPNL-LINK-1/resolve",
            json={"resolution_result": "YES", "linked_prediction_id": wrong_mkt["prediction_id"]},
        )
        assert res_wmkt.status_code == 409
        assert "does not match" in res_wmkt.json()["detail"].lower()

        # 3) Future forecast timestamp -> 400
        future_ts = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        future_pred = client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(forecast_timestamp=future_ts, external_forecast_id="ARENA-FUT"),
        ).json()["forecast"]
        res_fut = client.post(
            "/api/paper/blotter/PPNL-LINK-1/resolve",
            json={"resolution_result": "YES", "linked_prediction_id": future_pred["prediction_id"]},
        )
        assert res_fut.status_code == 400
        assert "future" in res_fut.json()["detail"].lower()

        # 4) Expired timeframe earlier than trade recorded_at -> 409
        expired_fc_ts = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
        expired_tf = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        expired_pred = client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(
                forecast_timestamp=expired_fc_ts,
                timeframe=expired_tf,
                external_forecast_id="ARENA-EXP-TF",
            ),
        ).json()["forecast"]
        res_exp = client.post(
            "/api/paper/blotter/PPNL-LINK-1/resolve",
            json={"resolution_result": "YES", "linked_prediction_id": expired_pred["prediction_id"]},
        )
        assert res_exp.status_code == 409
        assert "expired before" in res_exp.json()["detail"].lower()

        # 5) Valid open forecast tested against mismatched expected_timeframe, mismatched Arena model, and inverted pair
        valid_pred = client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(external_forecast_id="ARENA-VALID-1"),
        ).json()["forecast"]

        res_tf = client.post(
            "/api/paper/blotter/PPNL-LINK-1/resolve",
            json={
                "resolution_result": "YES",
                "linked_prediction_id": valid_pred["prediction_id"],
                "expected_timeframe": "2026-11-30T00:00:00Z",
            },
        )
        assert res_tf.status_code == 409
        assert "timeframe" in res_tf.json()["detail"].lower()

        res_model = client.post(
            "/api/paper/blotter/PPNL-LINK-1/resolve",
            json={
                "resolution_result": "YES",
                "linked_prediction_id": valid_pred["prediction_id"],
                "arena_model_id": "arena:anthropic/claude-spoofed",
            },
        )
        assert res_model.status_code == 409
        assert "attribution mismatch" in res_model.json()["detail"].lower()

        res_inv = client.post(
            "/api/paper/blotter/PPNL-INVERTED-1/resolve",
            json={
                "resolution_result": "YES",
                "linked_prediction_id": valid_pred["prediction_id"],
            },
        )
        assert res_inv.status_code == 409
        assert "inverted polarity" in res_inv.json()["detail"].lower()

        # 6) Valid resolution succeeds once, then rejects resolving same prediction again on ledger 2
        ok = client.post(
            "/api/paper/blotter/PPNL-LINK-1/resolve",
            json={
                "resolution_result": "YES",
                "resolution_source": "bls_cpi_official",
                "linked_prediction_id": valid_pred["prediction_id"],
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["entry"]["arena_model_id"] == "arena:gemini/gemini-2.5-pro"

        dup_pred = client.post(
            "/api/paper/blotter/PPNL-LINK-2/resolve",
            json={
                "resolution_result": "YES",
                "linked_prediction_id": valid_pred["prediction_id"],
            },
        )
        assert dup_pred.status_code == 409
        assert "already finalized" in dup_pred.json()["detail"].lower()


@pytest.mark.parametrize(
    "fault_stage",
    [
        "record_observation",
        "record_evaluation",
        "record_decision",
        "resolve_prediction",
        "record_outcome",
        "resolve_paper_pnl_entry",
    ],
)
def test_stage_failure_rolls_back_atomically_and_retry_succeeds_without_duplicates(
    monkeypatch, fault_stage: str
):
    app = _setup_app_with_verified_pair()
    _seed_paper_ledger(app, ledger_id="PPNL-ATOMIC-01")

    pred = app.state.prob_engine.ingest_arena_forecast(
        **_arena_payload(external_forecast_id=f"ARENA-ATOMIC-{fault_stage}")
    )
    pred_id = pred["prediction_id"]

    svc = app.state.blotter_resolution_service

    def _boom(*args, **kwargs):
        raise RuntimeError(f"injected_failure:{fault_stage}")

    if fault_stage in ("record_observation", "record_evaluation", "record_decision", "record_outcome"):
        monkeypatch.setattr(svc.learning_engine, fault_stage, _boom)
    elif fault_stage == "resolve_prediction":
        monkeypatch.setattr(svc.prob_engine, "resolve_prediction", _boom)
    elif fault_stage == "resolve_paper_pnl_entry":
        monkeypatch.setattr(svc.store, "resolve_paper_pnl_entry", _boom)

    with pytest.raises(RuntimeError, match=f"injected_failure:{fault_stage}"):
        svc.resolve_entry(
            ledger_id="PPNL-ATOMIC-01",
            resolution_result="YES",
            operator_username="audit_operator",
            resolution_source="official_exchange_oracle",
            notes="Should roll back completely",
            linked_prediction_id=pred_id,
        )

    # Assert ZERO partial updates across all tables after rollback
    entry_after_fail = app.state.store.get_paper_pnl_entry("PPNL-ATOMIC-01")
    assert entry_after_fail is not None
    assert entry_after_fail["resolution_result"] == "PENDING"
    assert entry_after_fail["learning_cycle_id"] is None
    assert entry_after_fail["linked_prediction_id"] is None

    pred_after_fail = app.state.prob_engine.get_prediction(pred_id)
    assert pred_after_fail is not None
    assert pred_after_fail["status"] == "OPEN"
    assert pred_after_fail["actual_outcome"] is None
    assert pred_after_fail["brier_score"] is None
    assert pred_after_fail["learning_cycle_id"] is None

    cycles_after_fail = app.state.learning_engine.list_cycles(
        source_type="PAPER_BLOTTER_RESOLUTION", limit=50
    )
    assert cycles_after_fail == []

    # Undo fault injection and verify clean retry produces exactly 1 cycle & full persistence
    monkeypatch.undo()
    retry = svc.resolve_entry(
        ledger_id="PPNL-ATOMIC-01",
        resolution_result="YES",
        operator_username="audit_operator",
        resolution_source="official_exchange_oracle",
        notes="Clean retry after transient failure",
        linked_prediction_id=pred_id,
    )
    assert retry["entry"]["resolution_result"] == "YES"
    assert retry["entry"]["resolved_by"] == "audit_operator"
    assert retry["entry"]["resolution_source"] == "official_exchange_oracle"
    assert retry["resolved_prediction"]["status"] == "RESOLVED"
    assert retry["resolved_prediction"]["brier_score"] == pytest.approx(0.04, abs=1e-4)

    cycles_after_retry = app.state.learning_engine.list_cycles(
        source_type="PAPER_BLOTTER_RESOLUTION", limit=50
    )
    assert len(cycles_after_retry) == 1
    assert cycles_after_retry[0]["cycle_id"] == retry["learning_cycle_id"]
    assert retry["resolved_prediction"]["learning_cycle_id"] == retry["learning_cycle_id"]

    # Confirm no second PREDICTION_ERROR cycle was duplicated
    all_cycles = app.state.learning_engine.list_cycles(limit=50)
    assert len(all_cycles) == 1
    app.state.store.close()


def test_arena_forecast_ingestion_single_batch_dedup_leaderboard_and_rbac():
    app = _setup_app_with_verified_pair()
    _seed_paper_ledger(app, ledger_id="PPNL-ARENA-E2E")

    with TestClient(app) as owner_client:
        authenticate_test_client(owner_client)

        # Create READ_ONLY and ANALYST accounts to verify RBAC
        for uname, role in [("ro_arena", "READ_ONLY"), ("an_arena", "ANALYST")]:
            u_res = owner_client.post(
                "/api/admin/users",
                json={
                    "username": uname,
                    "password": "Strong-Arena-Pass!2026",
                    "role": role,
                    "email": f"{uname}@certainty.local",
                },
            )
            assert u_res.status_code == 200, u_res.text

        # Anonymous denied (401)
        anon = TestClient(app)
        assert anon.post("/api/intelligence/arena/forecasts", json=_arena_payload()).status_code == 401
        assert anon.get("/api/intelligence/arena/forecasts").status_code == 401

        # READ_ONLY denied on POST (403), allowed on GET (200)
        ro_client = TestClient(app)
        authenticate_test_client(ro_client, username="ro_arena", password="Strong-Arena-Pass!2026")
        assert ro_client.post("/api/intelligence/arena/forecasts", json=_arena_payload()).status_code == 403
        assert ro_client.get("/api/intelligence/arena/forecasts").status_code == 200

        # ANALYST can ingest single Arena forecast with full metadata
        an_client = TestClient(app)
        authenticate_test_client(an_client, username="an_arena", password="Strong-Arena-Pass!2026")

        payload = _arena_payload(external_forecast_id="ARENA-EXT-001")
        r1 = an_client.post("/api/intelligence/arena/forecasts", json=payload)
        assert r1.status_code == 200, r1.text
        d1 = r1.json()
        assert d1["live_trading_enabled"] is False
        assert d1["deduplicated"] is False
        fc = d1["forecast"]
        assert fc["source_system"] == "AI_ORCHESTRA_ARENA"
        assert fc["agent_id"] == "arena:gemini/gemini-2.5-pro"
        assert fc["model_id"] == "gemini-2.5-pro"
        assert fc["provider"] == "gemini"
        assert fc["task_category"] == "prediction_market_binary"
        assert fc["prompt_version"] == "prompt-v2.1"
        assert fc["benchmark_version"] == "bench-2026.11"
        assert fc["market_id"] == "POLY-CPI-26"
        assert fc["timeframe"] == "2026-12-31T23:59:59Z"
        assert fc["dataset_snapshot_id"] == "SNAP-CPI-20261120"
        assert fc["predicted_prob"] == pytest.approx(0.80, abs=1e-4)
        assert fc["predicted_direction"] == "YES"
        assert fc["status"] == "OPEN"

        # Idempotent dedup on repeated external_forecast_id
        r1_dup = an_client.post("/api/intelligence/arena/forecasts", json=payload)
        assert r1_dup.status_code == 200
        assert r1_dup.json()["deduplicated"] is True
        assert r1_dup.json()["forecast"]["prediction_id"] == fc["prediction_id"]

        # Contradictory probability & direction rejected with 400
        bad_dir = an_client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(predicted_prob=0.22, predicted_direction="YES"),
        )
        assert bad_dir.status_code == 400
        assert "contradicts" in bad_dir.json()["detail"].lower()

        # Invalid ISO forecast_timestamp rejected with 400
        bad_ts = an_client.post(
            "/api/intelligence/arena/forecasts",
            json=_arena_payload(forecast_timestamp="not-a-timestamp"),
        )
        assert bad_ts.status_code == 400

        # Atomic batch rollback: valid item #1 + contradictory item #2 -> 400 & item #1 NOT persisted
        bad_batch = an_client.post(
            "/api/intelligence/arena/forecasts/batch",
            json={
                "forecasts": [
                    _arena_payload(
                        model_id="claude-3.7-sonnet",
                        provider="anthropic",
                        external_forecast_id="ARENA-BATCH-ROLLBACK-1",
                    ),
                    _arena_payload(
                        model_id="gpt-4.1",
                        provider="openai",
                        predicted_prob=0.85,
                        predicted_direction="NO",
                        external_forecast_id="ARENA-BATCH-ROLLBACK-2",
                    ),
                ]
            },
        )
        assert bad_batch.status_code == 400
        assert (
            app.state.prob_engine.get_prediction_by_external_id("ARENA-BATCH-ROLLBACK-1") is None
        )

        # Valid batch ingestion persists both forecasts into the unified predictions table
        good_batch = an_client.post(
            "/api/intelligence/arena/forecasts/batch",
            json={
                "forecasts": [
                    _arena_payload(
                        model_id="claude-3.7-sonnet",
                        provider="anthropic",
                        predicted_prob=0.72,
                        predicted_direction="YES",
                        external_forecast_id="ARENA-BATCH-OK-1",
                    ),
                    _arena_payload(
                        model_id="llama-3.3-70b",
                        provider="groq",
                        predicted_prob=0.30,
                        predicted_direction="NO",
                        external_forecast_id="ARENA-BATCH-OK-2",
                    ),
                ]
            },
        )
        assert good_batch.status_code == 200, good_batch.text
        assert good_batch.json()["inserted_count"] == 2

        # End-to-end: confirm paper blotter outcome YES linked to Arena forecast -> updates leaderboard
        confirm = owner_client.post(
            "/api/paper/blotter/PPNL-ARENA-E2E/resolve",
            json={
                "resolution_result": "YES",
                "resolution_source": "bls_cpi_dec_2026",
                "notes": "Verified official settlement.",
                "linked_prediction_id": fc["prediction_id"],
            },
        )
        assert confirm.status_code == 200, confirm.text
        assert confirm.json()["resolved_prediction"]["brier_score"] == pytest.approx(0.04, abs=1e-4)

        # Verify unified predictions table contains Arena forecast + leaderboard scores it
        listed = owner_client.get(
            "/api/intelligence/arena/forecasts",
            params={"provider": "gemini", "min_resolved_samples": 1},
        ).json()
        assert listed["live_trading_enabled"] is False
        assert listed["count"] == 1
        assert listed["forecasts"][0]["status"] == "RESOLVED"
        assert listed["forecasts"][0]["actual_outcome"] == 1
        assert listed["forecasts"][0]["brier_score"] == pytest.approx(0.04, abs=1e-4)

        lb_models = {m["model_id"]: m for m in listed["leaderboard"]["models"]}
        assert lb_models["gemini-2.5-pro"]["resolved_count"] == 1
        assert lb_models["gemini-2.5-pro"]["mean_brier_score"] == pytest.approx(0.04, abs=1e-4)
        assert lb_models["gemini-2.5-pro"]["comparison_eligible"] is True
        assert lb_models["claude-3.7-sonnet"]["open_count"] == 1
        assert lb_models["claude-3.7-sonnet"]["mean_brier_score"] is None

        # Verify Deterministic Risk Gate rules & live_trading_enabled remain completely unchanged
        dash = owner_client.get("/api/dashboard").json()
        assert dash["live_trading_enabled"] is False
        assert dash["risk_gate"]["fail_closed"] is True
        assert dash["risk_gate"]["policy"]["live_trading_enabled"] is False
        assert dash["risk_gate"]["policy"]["min_cross_venue_edge_pct"] == 2.0
