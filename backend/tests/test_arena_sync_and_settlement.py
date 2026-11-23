"""Comprehensive tests for Automated Arena Runner Sync, Dataset Split Enforcement,
Out-of-Sample Model Evaluation, and Read-Only Official Settlement Verification Queue.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.intelligence.arena_sync import (
    ArenaBenchmarkForecast,
    ArenaBenchmarkRun,
    ArenaExportAdapter,
    build_external_forecast_id,
    compute_snapshot_hash,
    run_deterministic_arena_benchmark,
)
from backend.main import create_app
from backend.security.config import (
    ARENA_SERVICE_TOKEN_HEADER,
    ARENA_SIGNATURE_HEADER,
    ARENA_TIMESTAMP_HEADER,
    SecuritySettings,
)
from backend.security.dependencies import compute_arena_request_signature
from backend.venues.settlement_verifier import (
    parse_kalshi_settlement,
    parse_polymarket_settlement,
)

OWNER_PASS = "CertStack!2026#ArenaSync99"
SERVICE_TOKEN = "arena-svc-token-super-secret-2026-xyz"
HMAC_SECRET = "arena-hmac-secret-super-secret-2026-abc"


def _make_sec_settings(
    *,
    service_token: str = SERVICE_TOKEN,
    hmac_secret: str = HMAC_SECRET,
) -> SecuritySettings:
    return SecuritySettings(
        env_mode="test",
        session_secret="a" * 48,
        require_tls_in_production=False,
        cookie_secure=False,
        cookie_samesite="lax",
        session_ttl_seconds=43200,
        rate_limit_enabled=False,
        allowed_origins=("http://localhost:3000", "http://testserver"),
        trusted_proxies=("127.0.0.1", "::1"),
        arena_service_token=service_token,
        arena_hmac_secret=hmac_secret,
    )


def _bootstrap_owner(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/api/auth/bootstrap-owner",
        json={
            "username": "owner",
            "password": OWNER_PASS,
            "email": "owner@certainty.local",
        },
    )
    assert resp.status_code == 200, resp.text
    csrf = resp.json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def _signed_service_headers(
    body_dict: dict[str, Any],
    *,
    token: str = SERVICE_TOKEN,
    hmac_secret: str = HMAC_SECRET,
    timestamp_override: int | None = None,
    signature_override: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    body_bytes = json.dumps(body_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        ARENA_SERVICE_TOKEN_HEADER: token,
    }
    if hmac_secret or signature_override is not None:
        ts_str = str(timestamp_override if timestamp_override is not None else int(time.time()))
        sig = (
            signature_override
            if signature_override is not None
            else compute_arena_request_signature(
                hmac_secret=hmac_secret,
                timestamp_str=ts_str,
                body_bytes=body_bytes,
            )
        )
        headers[ARENA_TIMESTAMP_HEADER] = ts_str
        headers[ARENA_SIGNATURE_HEADER] = sig
    return body_bytes, headers


def _sample_market_cases(count: int = 3) -> list[dict[str, Any]]:
    cases = []
    for i in range(count):
        cases.append(
            {
                "market_id": f"POLY-BTC-TEST-{i + 1:03d}",
                "venue": "polymarket",
                "task_category": "prediction_market_binary",
                "timeframe": "2026-12-31T23:59:59Z",
                "question": f"Will BTC close above ${90 + i * 5}k by year-end?",
                "signal_prob": 0.72 if i % 2 == 0 else 0.28,
                "market_implied_prob": 0.65 if i % 2 == 0 else 0.35,
            }
        )
    return cases


class _TestClientBridgeTransport(httpx.BaseTransport):
    """Bridges synchronous httpx.Client calls directly into FastAPI TestClient."""

    def __init__(self, tc: TestClient) -> None:
        self._tc = tc

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query.decode('utf-8')}"
        resp = self._tc.request(
            method=request.method,
            url=path,
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx.Response(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
            request=request,
        )


# ---------------------------------------------------------------------------
# 1. Arena Export Adapter: Pre-export gates, Service Auth, Retries, Idempotency
# ---------------------------------------------------------------------------


def test_arena_export_adapter_rejects_incomplete_failed_unscored_unverified_runs():
    called = False

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    adapter = ArenaExportAdapter(
        base_url="http://testserver",
        service_token=SERVICE_TOKEN,
        hmac_secret=HMAC_SECRET,
        transport=httpx.MockTransport(fail_if_called),
    )
    base_fc = ArenaBenchmarkForecast(
        model_id="gemini-2.5-pro",
        provider="gemini",
        task_category="prediction_market_binary",
        prompt_version="prompt-v2",
        benchmark_version="bench-v1",
        market_id="POLY-MKT-1",
        venue="polymarket",
        timeframe="2026-12-31T23:59:59Z",
        forecast_timestamp="2026-11-10T12:00:00Z",
        predicted_prob=0.75,
        predicted_direction="YES",
    )
    snap_hash = "sha256:" + hashlib.sha256(b"snap").hexdigest()

    bad_runs = [
        # Incomplete
        ArenaBenchmarkRun(
            run_id="RUN-INC",
            status="RUNNING",
            scored=True,
            verified=True,
            benchmark_version="bench-v1",
            dataset_snapshot_id="SNAP-1",
            dataset_version="ds-v1",
            snapshot_hash=snap_hash,
            dataset_split="TRAIN",
            window_start="2026-11-01T00:00:00Z",
            window_end="2026-11-10T00:00:00Z",
            forecasts=[base_fc],
        ),
        # Failed
        ArenaBenchmarkRun(
            run_id="RUN-FAIL",
            status="FAILED",
            scored=True,
            verified=True,
            benchmark_version="bench-v1",
            dataset_snapshot_id="SNAP-1",
            dataset_version="ds-v1",
            snapshot_hash=snap_hash,
            dataset_split="TRAIN",
            window_start="2026-11-01T00:00:00Z",
            window_end="2026-11-10T00:00:00Z",
            forecasts=[base_fc],
        ),
        # Unscored
        ArenaBenchmarkRun(
            run_id="RUN-UNSCORED",
            status="COMPLETED",
            scored=False,
            verified=True,
            benchmark_version="bench-v1",
            dataset_snapshot_id="SNAP-1",
            dataset_version="ds-v1",
            snapshot_hash=snap_hash,
            dataset_split="TRAIN",
            window_start="2026-11-01T00:00:00Z",
            window_end="2026-11-10T00:00:00Z",
            forecasts=[base_fc],
        ),
        # Unverified
        ArenaBenchmarkRun(
            run_id="RUN-UNVERIFIED",
            status="COMPLETED",
            scored=True,
            verified=False,
            benchmark_version="bench-v1",
            dataset_snapshot_id="SNAP-1",
            dataset_version="ds-v1",
            snapshot_hash=snap_hash,
            dataset_split="TRAIN",
            window_start="2026-11-01T00:00:00Z",
            window_end="2026-11-10T00:00:00Z",
            forecasts=[base_fc],
        ),
        # Future-data lookahead leakage (window_end > forecast_timestamp)
        ArenaBenchmarkRun(
            run_id="RUN-LEAK",
            status="COMPLETED",
            scored=True,
            verified=True,
            benchmark_version="bench-v1",
            dataset_snapshot_id="SNAP-1",
            dataset_version="ds-v1",
            snapshot_hash=snap_hash,
            dataset_split="TRAIN",
            window_start="2026-11-01T00:00:00Z",
            window_end="2026-11-15T00:00:00Z",
            forecasts=[base_fc],
        ),
    ]

    for bad_run in bad_runs:
        res = adapter.export_run(bad_run)
        assert res.exported is False
        assert res.status == "REJECTED_PRE_EXPORT"
        assert res.attempts == 0
        assert res.inserted_count == 0
    assert called is False


def test_arena_service_auth_rejections_and_hmac_replay_protection():
    # 1. Server with unconfigured service token rejects service header
    app_no_token = create_app(db_path=":memory:", security_settings=_make_sec_settings(service_token="", hmac_secret=""))
    with TestClient(app_no_token) as tc_no_token:
        payload = {"forecasts": []}
        body, headers = _signed_service_headers(payload, token=SERVICE_TOKEN, hmac_secret="")
        resp = tc_no_token.post("/api/intelligence/arena/forecasts/batch", content=body, headers=headers)
        assert resp.status_code == 401

    # 2. Server with token + HMAC enforces exact token, signature, and timestamp window
    app = create_app(db_path=":memory:", security_settings=_make_sec_settings())
    with TestClient(app) as tc:
        snap_payload = {
            "dataset_snapshot_id": "SNAP-AUTH-1",
            "dataset_version": "ds-v1",
            "snapshot_hash": "sha256:" + hashlib.sha256(b"auth").hexdigest(),
            "split": "TRAIN",
            "window_start": "2026-11-01T00:00:00Z",
            "window_end": "2026-11-05T00:00:00Z",
        }

        # Wrong token -> 401
        body, headers = _signed_service_headers(snap_payload, token="wrong-token")
        r1 = tc.post("/api/intelligence/datasets/snapshots", content=body, headers=headers)
        assert r1.status_code == 401

        # Valid token, bad HMAC signature -> 401
        body, headers = _signed_service_headers(snap_payload, signature_override="deadbeef" * 8)
        r2 = tc.post("/api/intelligence/datasets/snapshots", content=body, headers=headers)
        assert r2.status_code == 401

        # Valid token, expired timestamp (>300s replay window) -> 401
        stale_ts = int(time.time()) - 900
        body, headers = _signed_service_headers(snap_payload, timestamp_override=stale_ts)
        r3 = tc.post("/api/intelligence/datasets/snapshots", content=body, headers=headers)
        assert r3.status_code == 401

        # Valid token + valid HMAC -> 200
        body, headers = _signed_service_headers(snap_payload)
        r4 = tc.post("/api/intelligence/datasets/snapshots", content=body, headers=headers)
        assert r4.status_code == 200, r4.text
        assert r4.json()["snapshot"]["dataset_snapshot_id"] == "SNAP-AUTH-1"

        # Verify security audit logs captured the denied attempts
        owner_headers = _bootstrap_owner(tc)
        audit_resp = tc.get("/api/admin/audit-logs?limit=20", headers=owner_headers)
        assert audit_resp.status_code == 200
        denied_events = [
            e for e in audit_resp.json()["items"] if e["event_type"] == "ARENA_SERVICE_AUTH_DENIED"
        ]
        assert len(denied_events) >= 3


def test_arena_export_adapter_retries_transient_failures_and_is_idempotent():
    app = create_app(db_path=":memory:", security_settings=_make_sec_settings())
    with TestClient(app) as tc:
        bridge = _TestClientBridgeTransport(tc)
        call_counts = {"batch": 0}
        sleeps: list[float] = []

        class FlakyTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                if request.url.path == "/api/intelligence/arena/forecasts/batch":
                    call_counts["batch"] += 1
                    if call_counts["batch"] == 1:
                        return httpx.Response(503, text="Upstream temporarily overloaded", request=request)
                    if call_counts["batch"] == 2:
                        raise httpx.ConnectTimeout("simulated transient timeout")
                return bridge.handle_request(request)

        run = run_deterministic_arena_benchmark(
            run_id="ARENA-RUN-RETRY-01",
            benchmark_version="bench-2026.11",
            dataset_snapshot_id="SNAP-TRAIN-001",
            dataset_version="ds-2026.11",
            dataset_split="TRAIN",
            window_start="2026-10-01T00:00:00Z",
            window_end="2026-10-31T23:59:59Z",
            forecast_timestamp="2026-11-01T01:00:00Z",
            market_cases=_sample_market_cases(2),
            models=[
                {"provider": "gemini", "model_id": "gemini-2.5-pro", "prompt_version": "p-v1", "arena_routing_score": 0.88},
            ],
        )

        adapter = ArenaExportAdapter(
            base_url="http://testserver",
            service_token=SERVICE_TOKEN,
            hmac_secret=HMAC_SECRET,
            max_retries=3,
            backoff_base_seconds=0.1,
            transport=FlakyTransport(),
            sleep_fn=lambda s: sleeps.append(s),
        )
        first = adapter.export_run(run)
        assert first.exported is True
        assert first.inserted_count == 2
        assert first.deduplicated_count == 0
        assert call_counts["batch"] == 3
        assert sleeps == [0.1, 0.2]

        # Re-exporting identical run must be 100% idempotent (0 new rows, 2 deduplicated)
        clean_adapter = ArenaExportAdapter(
            base_url="http://testserver",
            service_token=SERVICE_TOKEN,
            hmac_secret=HMAC_SECRET,
            transport=bridge,
            sleep_fn=lambda _: None,
        )
        second = clean_adapter.export_run(run)
        assert second.exported is True
        assert second.inserted_count == 0
        assert second.deduplicated_count == 2
        assert second.prediction_ids == first.prediction_ids

        # Permanent 401 auth failure must fail fast (1 attempt, no retries)
        bad_adapter = ArenaExportAdapter(
            base_url="http://testserver",
            service_token="invalid-token",
            hmac_secret=HMAC_SECRET,
            max_retries=4,
            transport=bridge,
            sleep_fn=lambda _: None,
        )
        bad_res = bad_adapter.export_run(run)
        assert bad_res.exported is False
        assert bad_res.http_status == 401
        assert bad_res.attempts == 1


# ---------------------------------------------------------------------------
# 2. Dataset Split Enforcement, Leakage Detection & TEST-Set Quarantine
# ---------------------------------------------------------------------------


def test_dataset_splits_overlap_leakage_staleness_and_test_quarantine():
    app = create_app(db_path=":memory:", security_settings=_make_sec_settings())
    with TestClient(app) as tc:
        csrf = _bootstrap_owner(tc)

        # Register clean non-overlapping TRAIN, VALIDATION, TEST splits
        train_hash = "sha256:" + hashlib.sha256(b"train-v1").hexdigest()
        val_hash = "sha256:" + hashlib.sha256(b"val-v1").hexdigest()
        test_hash = "sha256:" + hashlib.sha256(b"test-v1").hexdigest()

        for snap in [
            {
                "dataset_snapshot_id": "SNAP-TR-1",
                "dataset_version": "ds-2026.11",
                "snapshot_hash": train_hash,
                "split": "TRAIN",
                "window_start": "2026-09-01T00:00:00Z",
                "window_end": "2026-09-30T23:59:59Z",
            },
            {
                "dataset_snapshot_id": "SNAP-VA-1",
                "dataset_version": "ds-2026.11",
                "snapshot_hash": val_hash,
                "split": "VALIDATION",
                "window_start": "2026-10-01T00:00:00Z",
                "window_end": "2026-10-31T23:59:59Z",
            },
            {
                "dataset_snapshot_id": "SNAP-TE-1",
                "dataset_version": "ds-2026.11",
                "snapshot_hash": test_hash,
                "split": "TEST",
                "window_start": "2026-11-01T00:00:00Z",
                "window_end": "2026-11-15T23:59:59Z",
            },
        ]:
            r = tc.post("/api/intelligence/datasets/snapshots", json=snap, headers=csrf)
            assert r.status_code == 200, r.text

        # Overlapping window across splits -> 409
        overlap_resp = tc.post(
            "/api/intelligence/datasets/snapshots",
            json={
                "dataset_snapshot_id": "SNAP-OVERLAP",
                "dataset_version": "ds-2026.11",
                "snapshot_hash": "sha256:" + hashlib.sha256(b"overlap").hexdigest(),
                "split": "VALIDATION",
                "window_start": "2026-09-25T00:00:00Z",
                "window_end": "2026-10-05T00:00:00Z",
            },
            headers=csrf,
        )
        assert overlap_resp.status_code == 409
        assert "Overlapping" in overlap_resp.text

        # Chronological violation (TEST before VALIDATION) -> 409
        chrono_resp = tc.post(
            "/api/intelligence/datasets/snapshots",
            json={
                "dataset_snapshot_id": "SNAP-EARLY-TEST",
                "dataset_version": "ds-2026.11",
                "snapshot_hash": "sha256:" + hashlib.sha256(b"earlytest").hexdigest(),
                "split": "TEST",
                "window_start": "2026-08-01T00:00:00Z",
                "window_end": "2026-08-20T00:00:00Z",
            },
            headers=csrf,
        )
        assert chrono_resp.status_code == 409

        base_item = {
            "model_id": "gemini-2.5-pro",
            "provider": "gemini",
            "task_category": "prediction_market_binary",
            "prompt_version": "prompt-v2",
            "benchmark_version": "bench-2026.11",
            "market_id": "POLY-LEAK-1",
            "venue": "polymarket",
            "timeframe": "2026-12-31T23:59:59Z",
            "forecast_timestamp": "2026-11-16T02:00:00Z",
            "dataset_snapshot_id": "SNAP-TE-1",
            "dataset_version": "ds-2026.11",
            "snapshot_hash": test_hash,
            "dataset_split": "TEST",
            "predicted_prob": 0.85,
            "predicted_direction": "YES",
            "require_registered_snapshot": True,
        }

        # Mismatched snapshot hash -> 400
        bad_hash = {**base_item, "snapshot_hash": "sha256:" + "0" * 64}
        r = tc.post("/api/intelligence/arena/forecasts", json=bad_hash, headers=csrf)
        assert r.status_code == 400
        assert "mismatch" in r.text.lower()

        # Mismatched dataset_split -> 400
        bad_split = {**base_item, "dataset_split": "TRAIN"}
        r = tc.post("/api/intelligence/arena/forecasts", json=bad_split, headers=csrf)
        assert r.status_code == 400
        assert "mismatch" in r.text.lower() and "split" in r.text.lower()

        # Future-data leakage (window_end 2026-11-15T23:59:59Z > forecast_timestamp 2026-11-10) -> 400
        future_leak = {**base_item, "forecast_timestamp": "2026-11-10T00:00:00Z"}
        r = tc.post("/api/intelligence/arena/forecasts", json=future_leak, headers=csrf)
        assert r.status_code == 400
        assert "leakage" in r.text.lower()

        # Target/post-cutoff leakage (timeframe <= window_end) -> 400
        target_leak = {**base_item, "timeframe": "2026-11-14T00:00:00Z"}
        r = tc.post("/api/intelligence/arena/forecasts", json=target_leak, headers=csrf)
        assert r.status_code == 400
        assert ("post-cutoff" in r.text.lower()) or ("leakage" in r.text.lower())

        # Stale forecast relative to snapshot window (> max_snapshot_lag_hours) -> 400
        stale_snap = {
            **base_item,
            "forecast_timestamp": "2026-12-10T00:00:00Z",
            "max_snapshot_lag_hours": 48.0,
        }
        r = tc.post("/api/intelligence/arena/forecasts", json=stale_snap, headers=csrf)
        assert r.status_code == 400
        assert "stale" in r.text.lower()

        # Stale live forecast (enforce_freshness=True with old timestamp) -> 400
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        stale_live_snap_hash = "sha256:" + hashlib.sha256(b"old-snap").hexdigest()
        tc.post(
            "/api/intelligence/datasets/snapshots",
            json={
                "dataset_snapshot_id": "SNAP-OLD-LIVE",
                "dataset_version": "ds-live-check",
                "snapshot_hash": stale_live_snap_hash,
                "split": "VALIDATION",
                "window_start": (datetime.now(timezone.utc) - timedelta(days=25)).isoformat(),
                "window_end": (datetime.now(timezone.utc) - timedelta(days=21)).isoformat(),
            },
            headers=csrf,
        )
        stale_live = {
            **base_item,
            "dataset_snapshot_id": "SNAP-OLD-LIVE",
            "dataset_version": "ds-live-check",
            "snapshot_hash": stale_live_snap_hash,
            "dataset_split": "VALIDATION",
            "forecast_timestamp": old_ts,
            "timeframe": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "enforce_freshness": True,
            "max_live_forecast_age_hours": 24.0,
        }
        r = tc.post("/api/intelligence/arena/forecasts", json=stale_live, headers=csrf)
        assert r.status_code == 400
        assert "stale" in r.text.lower()

        # Valid TEST-split forecast succeeds
        ok_resp = tc.post("/api/intelligence/arena/forecasts", json=base_item, headers=csrf)
        assert ok_resp.status_code == 200, ok_resp.text
        pred_id = ok_resp.json()["forecast"]["prediction_id"]

        # Resolve TEST forecast as NO (severe error: p=0.85, y=0 -> Brier=0.7225)
        # MUST compute Brier/log_loss cleanly while NEVER writing a mistake learning cycle!
        res_resp = tc.post(
            f"/api/intelligence/predictions/{pred_id}/resolve",
            json={"outcome": 0, "resolution_source": "official_test_settlement"},
            headers=csrf,
        )
        assert res_resp.status_code == 200, res_resp.text
        resolved = res_resp.json()
        assert resolved["brier_score"] == pytest.approx(0.7225, abs=1e-4)
        assert resolved["learning_cycle_id"] is None

        cycles = tc.get("/api/learning/cycles", headers=csrf).json()["items"]
        assert not any(c.get("source_ref_id") == pred_id for c in cycles)

        # Routing fallback-order update referencing TEST split MUST be blocked (403)
        route_block_1 = tc.post(
            "/api/intelligence/providers/fallback-order",
            json={
                "fallback_order": ["deterministic", "gemini", "ollama"],
                "source_dataset_split": "TEST",
            },
            headers=csrf,
        )
        assert route_block_1.status_code == 403
        assert "TEST" in route_block_1.text

        # Routing update citing a TEST snapshot ID (even without explicit split) MUST be blocked (403)
        route_block_2 = tc.post(
            "/api/intelligence/providers/fallback-order",
            json={
                "fallback_order": ["deterministic", "gemini", "ollama"],
                "source_snapshot_id": "SNAP-TE-1",
            },
            headers=csrf,
        )
        assert route_block_2.status_code == 403

        # Config governance proposal citing TEST split or TEST snapshot MUST raise PermissionError
        with pytest.raises(PermissionError, match="TEST"):
            app.state.config_manager.propose_config_update(
                config_domain="MODEL_ROUTING",
                proposed_config={"alpha": 0.9, "dataset_split": "TEST"},
                proposed_by="owner",
                change_reason="Attempted tuning on test set",
            )
        with pytest.raises(PermissionError, match="TEST"):
            app.state.config_manager.propose_config_update(
                config_domain="CALIBRATION_PARAMS",
                proposed_config={"alpha": 0.9, "dataset_snapshot_id": "SNAP-TE-1"},
                proposed_by="owner",
                change_reason="Attempted tuning via test snapshot ID",
            )


# ---------------------------------------------------------------------------
# 3. Model-Specific Evaluation: Same-Snapshot Isolation & Routing Separation
# ---------------------------------------------------------------------------


def test_model_evaluation_same_snapshot_min_sample_and_routing_separation():
    app = create_app(db_path=":memory:", security_settings=_make_sec_settings())
    with TestClient(app) as tc:
        csrf = _bootstrap_owner(tc)
        bridge = _TestClientBridgeTransport(tc)
        adapter = ArenaExportAdapter(
            base_url="http://testserver",
            service_token=SERVICE_TOKEN,
            hmac_secret=HMAC_SECRET,
            transport=bridge,
            sleep_fn=lambda _: None,
        )

        # Cohort A: 4 identical-snapshot VALIDATION markets
        cases_a = _sample_market_cases(4)
        # Model A (well calibrated) has LOW arena_routing_score (0.42)
        # Model B (poorly calibrated) has HIGH arena_routing_score (0.97)
        run_a = run_deterministic_arena_benchmark(
            run_id="RUN-EVAL-A",
            benchmark_version="bench-2026.11",
            dataset_snapshot_id="SNAP-VAL-A",
            dataset_version="ds-eval-1",
            dataset_split="VALIDATION",
            window_start="2026-10-01T00:00:00Z",
            window_end="2026-10-15T00:00:00Z",
            forecast_timestamp="2026-10-16T00:00:00Z",
            market_cases=cases_a,
            models=[
                {
                    "provider": "gemini",
                    "model_id": "gemini-calibrated",
                    "prompt_version": "p-v1",
                    "calibration_bias": 0.12,
                    "arena_routing_score": 0.42,
                },
                {
                    "provider": "openai",
                    "model_id": "gpt-overconfident-router-fav",
                    "prompt_version": "p-v1",
                    "calibration_bias": -0.20,
                    "arena_routing_score": 0.97,
                },
            ],
        )
        res_a = adapter.export_run(run_a)
        assert res_a.exported is True

        # Cohort B: Different snapshot with a 3rd model (MUST NOT be mixed with Cohort A)
        cases_b = [
            {
                "market_id": "POLY-ETH-OTHER-1",
                "venue": "polymarket",
                "task_category": "prediction_market_binary",
                "timeframe": "2026-12-31T23:59:59Z",
                "question": "Will ETH close above $5k?",
                "signal_prob": 0.80,
                "market_implied_prob": 0.70,
            }
        ]
        run_b = run_deterministic_arena_benchmark(
            run_id="RUN-EVAL-B",
            benchmark_version="bench-2026.12",
            dataset_snapshot_id="SNAP-VAL-B",
            dataset_version="ds-eval-2",
            dataset_split="VALIDATION",
            window_start="2026-10-16T00:00:00Z",
            window_end="2026-10-30T00:00:00Z",
            forecast_timestamp="2026-10-31T00:00:00Z",
            market_cases=cases_b,
            models=[
                {
                    "provider": "anthropic",
                    "model_id": "claude-solo",
                    "prompt_version": "p-v1",
                    "calibration_bias": 0.10,
                    "arena_routing_score": 0.99,
                }
            ],
        )
        res_b = adapter.export_run(run_b)
        assert res_b.exported is True

        # Resolve all markets in Cohort A: even indices -> YES (1), odd indices -> NO (0)
        # Note: signal_prob was 0.72 for even, 0.28 for odd.
        # For even: calibrated predicts 0.84 (y=1 -> Brier 0.0256), router-fav predicts 0.52 (y=1 -> Brier 0.2304)
        # For odd: calibrated predicts 0.40 (y=0 -> Brier 0.16), router-fav predicts 0.08 (y=0 -> Brier 0.0064)
        # Let's make actual outcomes ALL YES (1) so gemini-calibrated strictly dominates!
        fcs = tc.get("/api/intelligence/arena/forecasts?limit=50", headers=csrf).json()["forecasts"]
        for fc in fcs:
            r = tc.post(
                f"/api/intelligence/predictions/{fc['prediction_id']}/resolve",
                json={"outcome": 1, "resolution_source": "verified_eval_fixture"},
                headers=csrf,
            )
            assert r.status_code == 200, r.text

        # With default min_resolved_samples=10, Cohort A (4 markets) MUST NOT be ranked
        report_strict = tc.get("/api/intelligence/arena/evaluation?min_resolved_samples=10", headers=csrf).json()
        assert report_strict["routing_score_excluded_from_ranking"] is True
        assert report_strict["test_split_quarantined_from_tuning"] is True
        cohort_a_strict = next(
            c for c in report_strict["same_snapshot_comparisons"] if c["dataset_snapshot_id"] == "SNAP-VAL-A"
        )
        assert cohort_a_strict["comparison_eligible"] is False
        assert all(m["rank"] is None for m in cohort_a_strict["models"])

        # With min_resolved_samples=4, Cohort A IS comparable on 4 identical-snapshot markets
        report_4 = tc.get("/api/intelligence/arena/evaluation?min_resolved_samples=4", headers=csrf).json()
        cohort_a = next(
            c for c in report_4["same_snapshot_comparisons"] if c["dataset_snapshot_id"] == "SNAP-VAL-A"
        )
        cohort_b = next(
            c for c in report_4["same_snapshot_comparisons"] if c["dataset_snapshot_id"] == "SNAP-VAL-B"
        )

        # Cohort B (different snapshot & 1 model) stays isolated and unranked
        assert cohort_b["comparison_eligible"] is False
        assert cohort_b["models_in_cohort"] == 1
        assert cohort_b["comparison_status"] == "SINGLE_MODEL_COHORT"

        # Cohort A ranks strictly by verified Brier (NOT arena_routing_score)
        assert cohort_a["comparison_eligible"] is True
        assert cohort_a["matched_market_count"] == 4
        rank_1 = cohort_a["models"][0]
        rank_2 = cohort_a["models"][1]
        assert rank_1["rank"] == 1
        assert "gemini-calibrated" in rank_1["model_key"]
        assert rank_1["arena_routing_metadata"]["avg_arena_routing_score"] == pytest.approx(0.42, abs=1e-4)
        assert rank_1["arena_routing_metadata"]["excluded_from_verified_ranking"] is True

        assert rank_2["rank"] == 2
        assert "gpt-overconfident-router-fav" in rank_2["model_key"]
        assert rank_2["arena_routing_metadata"]["avg_arena_routing_score"] == pytest.approx(0.97, abs=1e-4)
        assert (
            rank_1["verified_forecast_metrics"]["mean_brier_score"]
            < rank_2["verified_forecast_metrics"]["mean_brier_score"]
        )
        assert rank_1["verified_forecast_metrics"]["expected_calibration_error"] is not None
        assert len(report_4["by_task_category"]) >= 1
        assert len(report_4["by_timeframe"]) >= 1
        assert len(report_4["by_split"]) >= 1


# ---------------------------------------------------------------------------
# 4. Settlement Verification Queue: Parsers, Conflicts, Operator Gate
# ---------------------------------------------------------------------------


def test_settlement_parsers_handle_finalized_disputed_ambiguous_unverified_and_conflicting():
    # Polymarket finalized YES
    pm_yes = parse_polymarket_settlement(
        "253591",
        {
            "id": "253591",
            "question": "Will X happen?",
            "closed": True,
            "umaResolutionStatus": "resolved",
            "umaResolutionStatuses": "[]",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
            "closedTime": "2024-11-06 15:17:41+00",
            "resolutionSource": "AP",
        },
    )
    assert pm_yes.proposed_outcome == "YES"
    assert pm_yes.verification_status == "PENDING_OPERATOR_REVIEW"
    assert pm_yes.conflict_reasons == []

    # Polymarket UMA DISPUTED -> blocked
    pm_disp = parse_polymarket_settlement(
        "999001",
        {
            "id": "999001",
            "closed": True,
            "umaResolutionStatus": "disputed",
            "umaResolutionStatuses": '["disputed"]',
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
        },
    )
    assert pm_disp.verification_status == "DISPUTED"
    assert pm_disp.proposed_outcome == "UNKNOWN"

    # Polymarket AMBIGUOUS 50/50 split -> blocked
    pm_amb = parse_polymarket_settlement(
        "999002",
        {
            "id": "999002",
            "closed": True,
            "umaResolutionStatus": "resolved",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
        },
    )
    assert pm_amb.verification_status == "AMBIGUOUS"

    # Polymarket OPEN / UNVERIFIED -> blocked
    pm_open = parse_polymarket_settlement("999003", {"id": "999003", "closed": False})
    assert pm_open.verification_status == "UNVERIFIED"

    # Kalshi finalized NO
    k_no = parse_kalshi_settlement(
        "KXTEST-NO",
        {
            "market": {
                "ticker": "KXTEST-NO",
                "status": "finalized",
                "result": "no",
                "settlement_value_dollars": "0.0000",
                "settlement_ts": "2026-11-01T12:00:00Z",
            }
        },
    )
    assert k_no.proposed_outcome == "NO"
    assert k_no.verification_status == "PENDING_OPERATOR_REVIEW"

    # Kalshi CONFLICTING_EVIDENCE (result='yes' vs settlement_value_dollars=0.0)
    k_conf = parse_kalshi_settlement(
        "KXTEST-CONF",
        {
            "market": {
                "ticker": "KXTEST-CONF",
                "status": "finalized",
                "result": "yes",
                "settlement_value_dollars": "0.0000",
                "settlement_ts": "2026-11-01T12:00:00Z",
            }
        },
    )
    assert k_conf.verification_status == "CONFLICTING_EVIDENCE"
    assert k_conf.proposed_outcome == "UNKNOWN"
    assert len(k_conf.conflict_reasons) == 1


def test_settlement_queue_blocks_conflicting_and_disputed_and_requires_operator_approval():
    app = create_app(db_path=":memory:", security_settings=_make_sec_settings())

    # Inject deterministic venue raw responses directly on the app's read-only clients
    poly_payloads: dict[str, dict[str, Any]] = {
        "PM-VERIFIED-YES": {
            "id": "PM-VERIFIED-YES",
            "question": "Will CPI fall below 2.5%?",
            "closed": True,
            "umaResolutionStatus": "resolved",
            "umaResolutionStatuses": "[]",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["1", "0"]',
            "closedTime": "2026-11-18 14:00:00+00",
            "resolutionSource": "BLS Official Release",
        },
        "PM-DISPUTED": {
            "id": "PM-DISPUTED",
            "question": "Disputed market",
            "closed": True,
            "umaResolutionStatus": "disputed",
            "umaResolutionStatuses": '["disputed"]',
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
        },
    }
    kalshi_payloads: dict[str, dict[str, Any]] = {
        "KX-CONFLICT-NO": {
            "market": {
                "ticker": "KX-CONFLICT-NO",
                "title": "CPI below 2.5%?",
                "status": "finalized",
                "result": "no",
                "settlement_value_dollars": "0.0000",
                "settlement_ts": "2026-11-18T14:05:00Z",
            }
        }
    }

    async def fake_poly_raw(market_id: str) -> dict[str, Any] | None:
        return poly_payloads.get(market_id)

    async def fake_kalshi_raw(ticker: str) -> dict[str, Any] | None:
        return kalshi_payloads.get(ticker)

    app.state.poly_client.fetch_market_raw = fake_poly_raw  # type: ignore[method-assign]
    app.state.kalshi_client.fetch_market_raw = fake_kalshi_raw  # type: ignore[method-assign]

    with TestClient(app) as tc:
        csrf = _bootstrap_owner(tc)

        # Ingest an OPEN Arena forecast
        fc_resp = tc.post(
            "/api/intelligence/arena/forecasts",
            json={
                "model_id": "gemini-2.5-pro",
                "provider": "gemini",
                "task_category": "macro_cpi",
                "prompt_version": "p-v2",
                "benchmark_version": "bench-v1",
                "market_id": "PM-VERIFIED-YES",
                "venue": "polymarket",
                "timeframe": "2026-12-31T23:59:59Z",
                "forecast_timestamp": "2026-11-10T10:00:00Z",
                "dataset_snapshot_id": "SNAP-CPI-1",
                "dataset_split": "VALIDATION",
                "predicted_prob": 0.80,
                "predicted_direction": "YES",
            },
            headers=csrf,
        )
        assert fc_resp.status_code == 200, fc_resp.text
        pred_id = fc_resp.json()["forecast"]["prediction_id"]

        # 1. Cross-venue conflicting evidence (Polymarket YES vs Kalshi NO) -> CONFLICTING_EVIDENCE
        conf_resp = tc.post(
            "/api/settlement/queue/fetch",
            json={
                "venue": "polymarket",
                "market_id": "PM-VERIFIED-YES",
                "secondary_venue": "kalshi",
                "secondary_market_id": "KX-CONFLICT-NO",
                "linked_prediction_id": pred_id,
            },
            headers=csrf,
        )
        assert conf_resp.status_code == 200, conf_resp.text
        conf_item = conf_resp.json()["queue_item"]
        assert conf_resp.json()["auto_finalized"] is False
        assert conf_item["verification_status"] == "CONFLICTING_EVIDENCE"
        assert conf_item["proposed_outcome"] == "UNKNOWN"
        assert len(conf_item["conflict_reasons"]) >= 1

        # Trying to APPROVE conflicting item MUST fail closed (409)
        approve_conf = tc.post(
            f"/api/settlement/queue/{conf_item['queue_id']}/review",
            json={"action": "APPROVE", "operator_confirmed": True},
            headers=csrf,
        )
        assert approve_conf.status_code == 409

        # 2. Disputed market -> DISPUTED, blocked from approval (409)
        disp_resp = tc.post(
            "/api/settlement/queue/fetch",
            json={"venue": "polymarket", "market_id": "PM-DISPUTED", "linked_prediction_id": pred_id},
            headers=csrf,
        )
        assert disp_resp.status_code == 200
        disp_item = disp_resp.json()["queue_item"]
        assert disp_item["verification_status"] == "DISPUTED"
        approve_disp = tc.post(
            f"/api/settlement/queue/{disp_item['queue_id']}/review",
            json={"action": "APPROVE", "operator_confirmed": True},
            headers=csrf,
        )
        assert approve_disp.status_code == 409

        # Forecast MUST still be OPEN and unscored!
        still_open = app.state.prob_engine.get_prediction(pred_id)
        assert still_open is not None
        assert still_open["status"] == "OPEN"
        assert still_open["brier_score"] is None

        # 3. Clean verified Polymarket settlement -> PENDING_OPERATOR_REVIEW (still NOT auto-finalized)
        ok_q = tc.post(
            "/api/settlement/queue/fetch",
            json={"venue": "polymarket", "market_id": "PM-VERIFIED-YES", "linked_prediction_id": pred_id},
            headers=csrf,
        )
        assert ok_q.status_code == 200, ok_q.text
        q_item = ok_q.json()["queue_item"]
        assert q_item["verification_status"] == "PENDING_OPERATOR_REVIEW"
        assert q_item["proposed_outcome"] == "YES"
        assert q_item["settlement_timestamp"] is not None

        # Still OPEN before operator approval!
        still_open_2 = app.state.prob_engine.get_prediction(pred_id)
        assert still_open_2 is not None and still_open_2["status"] == "OPEN"

        # Approval without operator_confirmed=True -> 400
        no_confirm = tc.post(
            f"/api/settlement/queue/{q_item['queue_id']}/review",
            json={"action": "APPROVE", "operator_confirmed": False},
            headers=csrf,
        )
        assert no_confirm.status_code == 400

        # Explicit operator approval -> atomically finalizes queue item & scores prediction!
        approved = tc.post(
            f"/api/settlement/queue/{q_item['queue_id']}/review",
            json={
                "action": "APPROVE",
                "operator_confirmed": True,
                "notes": "Verified against official BLS release.",
            },
            headers=csrf,
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["queue_item"]["verification_status"] == "APPROVED_AND_FINALIZED"
        assert body["queue_item"]["reviewed_by"] == "owner"
        assert body["resolved_prediction"]["status"] == "RESOLVED"
        assert body["resolved_prediction"]["actual_outcome"] == 1
        assert body["resolved_prediction"]["brier_score"] == pytest.approx(0.04, abs=1e-4)

        # Duplicate approval of same queue item -> 409
        dup_app = tc.post(
            f"/api/settlement/queue/{q_item['queue_id']}/review",
            json={"action": "APPROVE", "operator_confirmed": True},
            headers=csrf,
        )
        assert dup_app.status_code == 409


# ---------------------------------------------------------------------------
# 5. Real Isolated-DB End-to-End Integration Verification
# ---------------------------------------------------------------------------


def test_real_isolated_duckdb_e2e_arena_run_to_verified_settlement_score(tmp_path: Path):
    """End-to-end verification on a real isolated file-backed DuckDB database:
    Arena benchmark run -> Authenticated HMAC Export Adapter -> Cripto Brain OPEN forecast ->
    Official Settlement Queue -> Operator Approval -> Verified Brier/Log-Loss Score.
    """
    db_file = tmp_path / "isolated_arena_e2e.duckdb"
    app = create_app(db_path=str(db_file), security_settings=_make_sec_settings())

    # Inject official resolved Polymarket market response matching live Gamma schema
    async def mock_official_poly(market_id: str) -> dict[str, Any] | None:
        if market_id == "253591":
            return {
                "id": "253591",
                "conditionId": "0x253591cond",
                "question": "Real Isolated E2E Binary Market",
                "closed": True,
                "umaResolutionStatus": "resolved",
                "umaResolutionStatuses": "[]",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["1", "0"]',
                "closedTime": "2026-11-18 15:17:41+00",
                "resolutionSource": "Associated Press",
                "automaticallyResolved": True,
            }
        return None

    app.state.poly_client.fetch_market_raw = mock_official_poly  # type: ignore[method-assign]

    with TestClient(app) as tc:
        csrf = _bootstrap_owner(tc)
        bridge = _TestClientBridgeTransport(tc)

        # 1. Execute a real deterministic Arena benchmark run
        run = run_deterministic_arena_benchmark(
            run_id="ARENA-E2E-ISOLATED-001",
            benchmark_version="bench-e2e-v1",
            dataset_snapshot_id="SNAP-E2E-VAL-01",
            dataset_version="ds-e2e-2026.11",
            dataset_split="VALIDATION",
            window_start="2026-10-01T00:00:00Z",
            window_end="2026-11-01T00:00:00Z",
            forecast_timestamp="2026-11-02T00:00:00Z",
            market_cases=[
                {
                    "market_id": "253591",
                    "venue": "polymarket",
                    "task_category": "prediction_market_binary",
                    "timeframe": "2026-12-31T23:59:59Z",
                    "question": "Real Isolated E2E Binary Market",
                    "signal_prob": 0.78,
                    "market_implied_prob": 0.70,
                }
            ],
            models=[
                {
                    "provider": "gemini",
                    "model_id": "gemini-2.5-pro",
                    "prompt_version": "prompt-v3",
                    "calibration_bias": 0.02,
                    "arena_routing_score": 0.91,
                }
            ],
        )

        # 2. Export via authenticated ArenaExportAdapter (service token + HMAC)
        adapter = ArenaExportAdapter(
            base_url="http://testserver",
            service_token=SERVICE_TOKEN,
            hmac_secret=HMAC_SECRET,
            transport=bridge,
            sleep_fn=lambda _: None,
        )
        exp = adapter.export_run(run)
        assert exp.exported is True
        assert exp.inserted_count == 1
        assert len(exp.prediction_ids) == 1
        pred_id = exp.prediction_ids[0]

        # 3. Verify forecast arrived in Cripto Brain linked correctly and UNSCORED (OPEN)
        fcs = tc.get("/api/intelligence/arena/forecasts?dataset_snapshot_id=SNAP-E2E-VAL-01", headers=csrf).json()
        assert fcs["count"] == 1
        fc = fcs["forecasts"][0]
        assert fc["prediction_id"] == pred_id
        assert fc["status"] == "OPEN"
        assert fc["brier_score"] is None
        assert fc["log_loss"] is None
        assert fc["dataset_split"] == "VALIDATION"
        assert fc["snapshot_hash"] == run.snapshot_hash
        assert fc["benchmark_run_id"] == "ARENA-E2E-ISOLATED-001"

        # 4. Retrieve official settlement into review queue -> STILL OPEN & UNSCORED
        q_resp = tc.post(
            "/api/settlement/queue/fetch",
            json={"venue": "polymarket", "market_id": "253591", "linked_prediction_id": pred_id},
            headers=csrf,
        )
        assert q_resp.status_code == 200, q_resp.text
        queue_id = q_resp.json()["queue_item"]["queue_id"]
        assert q_resp.json()["queue_item"]["verification_status"] == "PENDING_OPERATOR_REVIEW"
        assert app.state.prob_engine.get_prediction(pred_id)["status"] == "OPEN"  # type: ignore[index]

        # 5. Operator approves settlement -> Forecast receives verified score
        rev = tc.post(
            f"/api/settlement/queue/{queue_id}/review",
            json={"action": "APPROVE", "operator_confirmed": True, "notes": "Verified official E2E settlement"},
            headers=csrf,
        )
        assert rev.status_code == 200, rev.text
        resolved_fc = rev.json()["resolved_prediction"]
        assert resolved_fc["status"] == "RESOLVED"
        assert resolved_fc["actual_outcome"] == 1
        # predicted_prob = 0.78 + 0.02 = 0.80 -> Brier = (0.80 - 1)^2 = 0.04
        assert resolved_fc["brier_score"] == pytest.approx(0.04, abs=1e-4)
        assert resolved_fc["log_loss"] is not None

        # 6. Confirm Deterministic Risk Gate policy remains completely untouched (live_trading_enabled=False)
        dash = tc.get("/api/dashboard", headers=csrf).json()
        assert dash["live_trading_enabled"] is False
        assert dash["risk_gate"]["policy"]["live_trading_enabled"] is False

    # Verify persistence on disk in the isolated DuckDB file
    assert db_file.exists() and db_file.stat().st_size > 0
