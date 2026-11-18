"""Stage 4A Comprehensive Security Tests: Auth, Argon2id, Sessions, RBAC, CSRF, Origin/CORS, TLS, Rate Limits & Migration."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

os.environ["CERTAINTY_DUCKDB_PATH"] = ":memory:"

import duckdb
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.security.config import (
    SecuritySettings,
    load_security_settings,
    validate_security_settings,
)
from backend.storage.duckdb_store import DuckDBStore
from backend.tests.auth_helpers import authenticate_test_client, sync_csrf_header

OWNER_PW = "Owner-Strong-Pass!2026"
OP_PW = "Operator-Strong-Pass!2026"
AN_PW = "Analyst-Strong-Pass!2026"
RO_PW = "ReadOnly-Strong-Pass!2026"


def _provision_four_roles(client: TestClient) -> dict[str, dict]:
    """Bootstrap OWNER and provision OPERATOR, ANALYST, and READ_ONLY accounts."""
    owner_user = authenticate_test_client(client, username="owner_one", password=OWNER_PW)
    created: dict[str, dict] = {"OWNER": owner_user}
    for role, uname, pw in (
        ("OPERATOR", "operator_one", OP_PW),
        ("ANALYST", "analyst_one", AN_PW),
        ("READ_ONLY", "viewer_one", RO_PW),
    ):
        resp = client.post(
            "/api/admin/users",
            json={"username": uname, "password": pw, "role": role, "email": f"{uname}@certainty.local"},
        )
        assert resp.status_code == 200, resp.text
        created[role] = resp.json()["user"]
    return created


def _login_as(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    sync_csrf_header(client)
    return resp.json()["user"]


# ----------------------------------------------------------------------
# 1. Deny-by-Default & Safe Minimal Health
# ----------------------------------------------------------------------


def test_deny_by_default_unauthenticated_and_minimal_health(tmp_path):
    app = create_app(db_path=str(tmp_path / "deny.duckdb"), enable_background_loop=False)
    with TestClient(app) as client:
        # Unauthenticated /api/health returns only minimal non-sensitive liveness
        h = client.get("/api/health")
        assert h.status_code == 200
        hb = h.json()
        assert hb["status"] == "NOMINAL"
        assert hb["authenticated"] is False
        assert hb["live_trading_enabled"] is False
        assert "duckdb" not in hb
        assert "venues" not in hb

        # All protected GET & POST endpoints return 401 when unauthenticated
        for method, path in (
            ("GET", "/api/dashboard"),
            ("GET", "/api/opportunities"),
            ("GET", "/api/risk-gate/audit"),
            ("GET", "/api/memory/structured"),
            ("GET", "/api/agents/status"),
            ("GET", "/api/agents/tasks"),
            ("GET", "/api/intelligence/command-center"),
            ("GET", "/api/operations/health"),
            ("GET", "/api/paper/blotter"),
            ("GET", "/api/admin/users"),
            ("GET", "/api/admin/audit-logs"),
            ("POST", "/api/scan/polymarket"),
            ("POST", "/api/paper/execute"),
            ("POST", "/api/agents/dispatch"),
            ("POST", "/api/risk-gate/kill-switch"),
        ):
            r = client.request(method, path, json={} if method == "POST" else None)
            assert r.status_code == 401, f"Expected 401 for {method} {path}, got {r.status_code}"


# ----------------------------------------------------------------------
# 2. Argon2id, Password Strength & Initial OWNER Bootstrap
# ----------------------------------------------------------------------


def test_argon2id_hashing_strength_and_single_bootstrap(tmp_path):
    db_file = str(tmp_path / "boot.duckdb")
    app = create_app(db_path=db_file, enable_background_loop=False)
    with TestClient(app) as client:
        st0 = client.get("/api/auth/status").json()
        assert st0["authenticated"] is False
        assert st0["needs_owner_bootstrap"] is True

        # Weak passwords rejected
        weak = client.post(
            "/api/auth/bootstrap-owner",
            json={"username": "owner_x", "password": "short"},
        )
        assert weak.status_code == 422

        weak_classes = client.post(
            "/api/auth/bootstrap-owner",
            json={"username": "owner_x", "password": "alllowercasepasswordonly"},
        )
        assert weak_classes.status_code == 400

        # Valid initial OWNER succeeds
        ok = client.post(
            "/api/auth/bootstrap-owner",
            json={"username": "owner_x", "password": OWNER_PW, "email": "o@x.local"},
        )
        assert ok.status_code == 200
        assert ok.json()["enrolled"] is True
        assert ok.json()["user"]["role"] == "OWNER"

        # Verify Argon2id hash stored in DuckDB (never plaintext)
        row = app.state.store._conn.execute(
            "SELECT password_hash FROM users WHERE username = ?", ["owner_x"]
        ).fetchone()
        assert row is not None
        assert row[0].startswith("$argon2id$")
        assert OWNER_PW not in row[0]

        # Second bootstrap attempt blocked with 409 Conflict
        second = client.post(
            "/api/auth/bootstrap-owner",
            json={"username": "owner_y", "password": OWNER_PW},
        )
        assert second.status_code == 409


# ----------------------------------------------------------------------
# 3. Session Lifecycle, Cookie Attributes, Fixation Rotation & Lockout
# ----------------------------------------------------------------------


def test_session_lifecycle_fixation_rotation_expiration_lockout_and_deactivation(tmp_path):
    settings = SecuritySettings(
        env_mode="development",
        session_secret="a" * 48,
        cookie_secure=False,
        rate_limit_enabled=False,  # Test account lockout independently of IP rate limit
        max_failed_logins=3,
        lockout_minutes=15,
    )
    app = create_app(
        db_path=str(tmp_path / "sess.duckdb"),
        enable_background_loop=False,
        security_settings=settings,
    )
    with TestClient(app) as client:
        _provision_four_roles(client)

        # Verify HttpOnly on session cookie and JS-readable on CSRF cookie
        c1 = TestClient(app)
        login_r = c1.post("/api/auth/login", json={"username": "operator_one", "password": OP_PW})
        assert login_r.status_code == 200
        set_cookies = login_r.headers.get_list("set-cookie")
        sess_cookie_hdr = next(h for h in set_cookies if h.startswith("certainty_session="))
        csrf_cookie_hdr = next(h for h in set_cookies if h.startswith("certainty_csrf="))
        assert "HttpOnly" in sess_cookie_hdr
        assert "HttpOnly" not in csrf_cookie_hdr
        old_sess_id = login_r.json()["user"]["session_id"]

        # Re-login rotates session & revokes prior session (session fixation protection)
        login_r2 = c1.post("/api/auth/login", json={"username": "operator_one", "password": OP_PW})
        assert login_r2.status_code == 200
        new_sess_id = login_r2.json()["user"]["session_id"]
        assert new_sess_id != old_sess_id
        old_row = app.state.store._conn.execute(
            "SELECT revoked_at FROM sessions WHERE session_id = ?", [old_sess_id]
        ).fetchone()
        assert old_row is not None and old_row[0] is not None

        # Forced expiration revokes session
        expired_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with app.state.store._lock:
            app.state.store._conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE session_id = ?", [expired_iso, new_sess_id]
            )
        assert c1.get("/api/dashboard").status_code == 401

        # Brute-force lockout after 3 bad passwords
        c2 = TestClient(app)
        for _ in range(3):
            bad = c2.post("/api/auth/login", json={"username": "analyst_one", "password": "Wrong-Password!99"})
            assert bad.status_code == 401
        locked = c2.post("/api/auth/login", json={"username": "analyst_one", "password": AN_PW})
        assert locked.status_code == 401
        assert "locked" in locked.json()["detail"].lower()

        # Account deactivation immediately revokes active session
        c3 = TestClient(app)
        _login_as(c3, "viewer_one", RO_PW)
        assert c3.get("/api/dashboard").status_code == 200
        users = client.get("/api/admin/users").json()["items"]
        viewer_id = next(u["user_id"] for u in users if u["username"] == "viewer_one")
        deact = client.post(f"/api/admin/users/{viewer_id}/deactivate")
        assert deact.status_code == 200
        assert c3.get("/api/dashboard").status_code == 401

        # Logout revokes caller's session
        c4 = TestClient(app)
        _login_as(c4, "operator_one", OP_PW)
        assert c4.get("/api/dashboard").status_code == 200
        out = c4.post("/api/auth/logout")
        assert out.status_code == 200
        assert c4.get("/api/dashboard").status_code == 401


# ----------------------------------------------------------------------
# 4. Complete 4-Role RBAC Matrix & Privilege Escalation Prevention
# ----------------------------------------------------------------------


def test_four_role_rbac_matrix_and_escalation_prevention(tmp_path):
    settings = SecuritySettings(
        env_mode="development",
        session_secret="b" * 48,
        rate_limit_enabled=False,
    )
    app = create_app(
        db_path=str(tmp_path / "rbac.duckdb"),
        enable_background_loop=False,
        security_settings=settings,
    )
    with TestClient(app) as owner_c:
        users = _provision_four_roles(owner_c)

        ro_c = TestClient(app)
        _login_as(ro_c, "viewer_one", RO_PW)

        an_c = TestClient(app)
        _login_as(an_c, "analyst_one", AN_PW)

        op_c = TestClient(app)
        _login_as(op_c, "operator_one", OP_PW)

        # READ_ONLY can read dashboards/telemetry, blocked 403 from ANALYST+ writes
        assert ro_c.get("/api/dashboard").status_code == 200
        assert ro_c.get("/api/intelligence/command-center").status_code == 200
        assert ro_c.post("/api/memory", json={"category": "TEST", "title": "Read only write", "summary": "Should 403"}).status_code == 403
        assert ro_c.post("/api/risk-gate/evaluate", json={"venue": "polymarket", "arbitrage_type": "intra_venue", "strategy_tier": "tier1", "execution_mode": "PAPER_RESEARCH", "market_id": "M1", "requested_notional_usd": 10, "net_edge_pct": 2.0, "liquidity_participation_pct": 1.0, "data_age_ms": 100}).status_code == 403
        assert ro_c.post("/api/intelligence/predictions", json={"agent_id": "p", "market_id": "M1", "subject_key": "s", "question": "q?", "predicted_prob": 0.5}).status_code == 403
        assert ro_c.get("/api/admin/users").status_code == 403

        # ANALYST can submit research & dry-run risk evaluations, blocked 403 from OPERATOR+ actions
        assert an_c.post("/api/memory", json={"category": "CALIBRATION_NOTE", "title": "Analyst obs", "summary": "Verified sample check"}).status_code == 200
        assert an_c.post("/api/paper/execute", json={"pair_id": "X-1", "target_shares": 10}).status_code == 403
        assert an_c.post("/api/agents/dispatch", json={"max_tasks": 1}).status_code == 403
        assert an_c.post("/api/scan/polymarket", json={"limit": 2}).status_code == 403
        assert an_c.post("/api/risk-gate/kill-switch", json={"active": True, "reason": "Unauthorized"}).status_code == 403
        assert an_c.get("/api/admin/users").status_code == 403

        # OPERATOR can dispatch agents & scale workers, blocked 403 from OWNER-only controls
        assert op_c.post("/api/agents/dispatch", json={"max_tasks": 1}).status_code == 200
        assert op_c.post("/api/risk-gate/kill-switch", json={"active": True, "reason": "Operator blocked"}).status_code == 403
        assert op_c.post("/api/intelligence/configs/rollback", json={"config_domain": "AGENT_PIPELINE", "target_version": 1}).status_code == 403
        assert op_c.get("/api/admin/users").status_code == 403
        assert op_c.patch(f"/api/admin/users/{users['OPERATOR']['user_id']}/role", json={"role": "OWNER"}).status_code == 403

        # OWNER cannot self-demote or deactivate last active OWNER
        assert owner_c.patch(f"/api/admin/users/{users['OWNER']['user_id']}/role", json={"role": "ANALYST"}).status_code == 400
        assert owner_c.post(f"/api/admin/users/{users['OWNER']['user_id']}/deactivate").status_code == 400
        assert owner_c.post("/api/risk-gate/kill-switch", json={"active": True, "reason": "OWNER halt verified"}).status_code == 200
        assert owner_c.post("/api/risk-gate/kill-switch", json={"active": False, "reason": "OWNER resume verified"}).status_code == 200


# ----------------------------------------------------------------------
# 5. Caller-Asserted Actor Impersonation Prevention & Anti-Self-Approval
# ----------------------------------------------------------------------


def test_server_derived_actor_attribution_and_http_anti_self_approval(tmp_path):
    settings = SecuritySettings(env_mode="development", session_secret="c" * 48, rate_limit_enabled=False)
    app = create_app(db_path=str(tmp_path / "actor.duckdb"), enable_background_loop=False, security_settings=settings)
    with TestClient(app) as owner_c:
        _provision_four_roles(owner_c)
        an_c = TestClient(app)
        _login_as(an_c, "analyst_one", AN_PW)
        op_c = TestClient(app)
        _login_as(op_c, "operator_one", OP_PW)

        # Spoofed created_by/changed_by/actor are overridden with server principal
        task_res = op_c.post(
            "/api/agents/tasks",
            json={
                "task_type": "CHECK_FEED_HEALTH",
                "assigned_agent": "MARKET_INTELLIGENCE",
                "created_by": "spoofed_owner_admin",
            },
        )
        assert task_res.status_code == 200
        assert task_res.json()["task"]["created_by"] == "operator_one"

        mem_res = an_c.post(
            "/api/memory/structured",
            json={
                "memory_tier": "EPISODIC",
                "category": "SPOOF_TEST",
                "title": "Attribution check",
                "summary": "Verify server assigns source_agent/actor reliably.",
                "source_agent": "spoofed_root",
                "evidence": [{"source_type": "AUDIT", "source_ref_id": "A-1"}],
            },
        )
        assert mem_res.status_code == 200
        assert mem_res.json()["entry"]["source_agent"] == "user:analyst_one"

        # Config proposal by analyst_one -> recorded as analyst_one even if proposed_by spoofed
        reg = app.state.learning_engine.generate_regression_case(
            source_ref_id="SRC-4A",
            category="CONFIG",
            title="Config gate",
            description="Pass",
            input_fixture={"ok": True},
            expected_invariant={"ok": True},
        )
        app.state.learning_engine.run_regression_case(reg["case_id"], observed_output={"ok": True})

        prop = an_c.post(
            "/api/intelligence/configs/propose",
            json={
                "config_domain": "AGENT_PIPELINE",
                "proposed_config": {"default_max_concurrency": 3, "live_trading_enabled": False},
                "proposed_by": "spoofed_different_user",
                "change_reason": "Reduce concurrency",
                "regression_case_id": reg["case_id"],
            },
        )
        assert prop.status_code == 200
        cfg_id = prop.json()["config_version_id"]
        assert prop.json()["proposed_by"] == "analyst_one"

        # Even if a second OWNER was same person who proposed, self-activation fails;
        # here owner_one is distinct from analyst_one so OWNER activation succeeds & logs owner_one
        act = owner_c.post(
            f"/api/intelligence/configs/{cfg_id}/activate",
            json={"reviewed_by": "spoofed_reviewer"},
        )
        assert act.status_code == 200
        assert act.json()["reviewed_by"] == "owner_one"

        # Now owner_one proposes -> owner_one attempting to activate own proposal is blocked 403!
        owner_prop = owner_c.post(
            "/api/intelligence/configs/propose",
            json={
                "config_domain": "AGENT_PIPELINE",
                "proposed_config": {"default_max_concurrency": 2, "live_trading_enabled": False},
                "proposed_by": "fake_other_user",
                "change_reason": "Self-approval test",
                "regression_case_id": reg["case_id"],
            },
        ).json()
        self_act = owner_c.post(
            f"/api/intelligence/configs/{owner_prop['config_version_id']}/activate",
            json={"reviewed_by": "fake_other_reviewer"},
        )
        assert self_act.status_code == 403


# ----------------------------------------------------------------------
# 6. CSRF Double-Submit & Origin Validation
# ----------------------------------------------------------------------


def test_csrf_and_origin_validation_enforced(tmp_path):
    settings = SecuritySettings(
        env_mode="development",
        session_secret="d" * 48,
        allowed_origins=("http://localhost:3000", "http://testserver"),
        rate_limit_enabled=False,
    )
    app = create_app(db_path=str(tmp_path / "csrf.duckdb"), enable_background_loop=False, security_settings=settings)
    with TestClient(app) as client:
        authenticate_test_client(client)

        # Missing CSRF header -> 403
        saved_csrf = client.headers.pop("X-CSRF-Token", None)
        no_csrf = client.post("/api/memory", json={"category": "T", "title": "No CSRF", "summary": "Must fail 403"})
        assert no_csrf.status_code == 403
        assert "csrf" in no_csrf.json()["detail"].lower()

        # Mismatched CSRF header -> 403
        client.headers["X-CSRF-Token"] = "forged-csrf-token-value"
        bad_csrf = client.post("/api/memory", json={"category": "T", "title": "Bad CSRF", "summary": "Must fail 403"})
        assert bad_csrf.status_code == 403

        # Restore valid CSRF, supply disallowed Origin -> 403
        if saved_csrf:
            client.headers["X-CSRF-Token"] = saved_csrf
        bad_origin = client.post(
            "/api/memory",
            headers={"Origin": "https://evil.attacker.example"},
            json={"category": "T", "title": "Evil origin", "summary": "Must fail 403"},
        )
        assert bad_origin.status_code == 403

        # Valid CSRF + allowed Origin -> 200
        ok = client.post(
            "/api/memory",
            headers={"Origin": "http://localhost:3000"},
            json={"category": "VERIFIED_LESSON", "title": "Valid CSRF", "summary": "Allowed origin + valid CSRF"},
        )
        assert ok.status_code == 200


# ----------------------------------------------------------------------
# 7. Production Startup Validation, HTTPS Enforcement & Trusted Proxy Spoof Defense
# ----------------------------------------------------------------------


def test_production_startup_refusal_https_enforcement_and_trusted_proxy(tmp_path):
    # Weak secret refused
    with pytest.raises(RuntimeError, match="32 characters"):
        validate_security_settings(SecuritySettings(env_mode="production", session_secret="short", cookie_secure=True, allowed_origins=("https://app.example.com",)))

    # Placeholder secret refused
    with pytest.raises(RuntimeError, match="placeholder"):
        validate_security_settings(SecuritySettings(env_mode="production", session_secret="changeme-" + "x" * 30, cookie_secure=True, allowed_origins=("https://app.example.com",)))

    # Insecure cookie in production refused
    with pytest.raises(RuntimeError, match="CERTAINTY_COOKIE_SECURE"):
        validate_security_settings(SecuritySettings(env_mode="production", session_secret="z" * 48, cookie_secure=False, allowed_origins=("https://app.example.com",)))

    # Wildcard CORS in production refused
    with pytest.raises(RuntimeError, match="wildcard"):
        validate_security_settings(SecuritySettings(env_mode="production", session_secret="z" * 48, cookie_secure=True, allowed_origins=("*",)))

    # Non-HTTPS origin in production refused
    with pytest.raises(RuntimeError, match="https://"):
        validate_security_settings(SecuritySettings(env_mode="production", session_secret="z" * 48, cookie_secure=True, allowed_origins=("http://app.example.com",)))

    # Valid production config boots; HTTP blocked unless forwarded over https from trusted proxy
    prod_settings = SecuritySettings(
        env_mode="production",
        session_secret="p" * 48,
        cookie_secure=True,
        allowed_origins=("https://stack.example.com",),
        trusted_proxies=("127.0.0.1", "testclient"),
        bootstrap_token="prod-boot-secret-999",
        rate_limit_enabled=False,
    )
    app = create_app(db_path=str(tmp_path / "prod.duckdb"), enable_background_loop=False, security_settings=prod_settings)
    with TestClient(app, base_url="http://stack.example.com") as http_client:
        # Unencrypted HTTP refused
        blocked_http = http_client.get("/api/auth/status")
        assert blocked_http.status_code == 403

    # Untrusted peer CANNOT spoof X-Forwarded-Proto: https or X-Forwarded-For
    strict_proxy_settings = SecuritySettings(
        env_mode="production",
        session_secret="p" * 48,
        cookie_secure=True,
        allowed_origins=("https://stack.example.com",),
        trusted_proxies=("10.0.0.5",),  # testclient is NOT in trusted_proxies
        bootstrap_token="prod-boot-secret-999",
        rate_limit_enabled=False,
    )
    app_strict = create_app(db_path=str(tmp_path / "strict.duckdb"), enable_background_loop=False, security_settings=strict_proxy_settings)
    with TestClient(app_strict, base_url="http://stack.example.com") as spoof_client:
        spoofed = spoof_client.get(
            "/api/auth/status",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "10.0.0.5"},
        )
        assert spoofed.status_code == 403

    # Trusted proxy over https succeeds & requires bootstrap token in production
    app_https = create_app(db_path=str(tmp_path / "prod_https.duckdb"), enable_background_loop=False, security_settings=prod_settings)
    with TestClient(app_https, base_url="https://stack.example.com") as https_client:
        no_tok = https_client.post(
            "/api/auth/bootstrap-owner",
            headers={"Origin": "https://stack.example.com"},
            json={"username": "prod_owner", "password": OWNER_PW},
        )
        assert no_tok.status_code == 403
        ok_boot = https_client.post(
            "/api/auth/bootstrap-owner",
            headers={"Origin": "https://stack.example.com"},
            json={"username": "prod_owner", "password": OWNER_PW, "bootstrap_token": "prod-boot-secret-999"},
        )
        assert ok_boot.status_code == 200


# ----------------------------------------------------------------------
# 8. Persistent DuckDB Rate Limiter, Concurrency & Audit Logging
# ----------------------------------------------------------------------


def test_persistent_rate_limiting_concurrency_and_audit_log(tmp_path):
    settings = SecuritySettings(
        env_mode="development",
        session_secret="r" * 48,
        rate_limit_enabled=True,
        rate_limit_overrides={"PAPER_EXECUTE": (3, 60), "LOGIN_IP": (4, 60)},
    )
    app = create_app(db_path=str(tmp_path / "rl.duckdb"), enable_background_loop=False, security_settings=settings)

    # Atomic concurrency check directly on DuckDBRateLimiter (limit=3 across 10 threads)
    limiter = app.state.rate_limiter
    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(lambda _: limiter.check_and_consume("PAPER_EXECUTE", "concurrent-key"), range(10)))
    allowed = [d for d in decisions if d.allowed]
    denied = [d for d in decisions if not d.allowed]
    assert len(allowed) == 3
    assert len(denied) == 7

    # HTTP login rate limiting returns 429 + Retry-After and records RATE_LIMIT_BLOCKED audit event
    with TestClient(app) as client:
        _provision_four_roles(client)
        c = TestClient(app)
        codes = [
            c.post("/api/auth/login", json={"username": "nobody", "password": "Bad-Password!123"}).status_code
            for _ in range(5)
        ]
        assert codes.count(401) == 4
        assert codes.count(429) == 1

        logs = client.get("/api/admin/audit-logs?event_type=RATE_LIMIT_BLOCKED").json()["items"]
        assert len(logs) >= 1
        assert "Bad-Password" not in json.dumps(logs)


# ----------------------------------------------------------------------
# 9. Password Change, Single-Use Reset Token & Immutable Audit Log
# ----------------------------------------------------------------------


def test_password_change_single_use_reset_token_and_immutable_audit_log(tmp_path):
    settings = SecuritySettings(env_mode="development", session_secret="s" * 48, rate_limit_enabled=False)
    app = create_app(db_path=str(tmp_path / "reset.duckdb"), enable_background_loop=False, security_settings=settings)
    with TestClient(app) as owner_c:
        users = _provision_four_roles(owner_c)

        # Operator changes own password -> invalidates prior session & rotates active session
        op_c1 = TestClient(app)
        u_before = _login_as(op_c1, "operator_one", OP_PW)
        op_c2 = TestClient(app)
        _login_as(op_c2, "operator_one", OP_PW)

        new_op_pw = "Operator-Rotated-Pass!2027"
        cp = op_c2.post(
            "/api/auth/change-password",
            json={"current_password": OP_PW, "new_password": new_op_pw},
        )
        assert cp.status_code == 200
        assert cp.json()["user"]["session_id"] != u_before["session_id"]
        # Old password can no longer log in; new password works
        assert TestClient(app).post("/api/auth/login", json={"username": "operator_one", "password": OP_PW}).status_code == 401

        # OWNER issues single-use reset token for analyst_one
        an_c = TestClient(app)
        _login_as(an_c, "analyst_one", AN_PW)
        tok_resp = owner_c.post(
            f"/api/admin/users/{users['ANALYST']['user_id']}/reset-token",
            json={"ttl_minutes": 30},
        )
        assert tok_resp.status_code == 200
        reset_token = tok_resp.json()["reset_token"]

        # Confirm reset -> revokes analyst_one's active session
        new_an_pw = "Analyst-Reset-Pass!2027"
        conf = TestClient(app).post(
            "/api/auth/reset-password/confirm",
            json={"reset_token": reset_token, "new_password": new_an_pw},
        )
        assert conf.status_code == 200
        assert conf.json()["reset_completed"] is True
        assert conf.json()["sessions_revoked"] >= 1
        assert an_c.get("/api/dashboard").status_code == 401

        # Second reuse of same reset token fails closed
        reuse = TestClient(app).post(
            "/api/auth/reset-password/confirm",
            json={"reset_token": reset_token, "new_password": "Another-Password!2028"},
        )
        assert reuse.status_code == 400

        # Expired reset token fails closed
        tok2 = owner_c.post(
            f"/api/admin/users/{users['ANALYST']['user_id']}/reset-token",
            json={"ttl_minutes": 10},
        ).json()
        with app.state.store._lock:
            app.state.store._conn.execute(
                "UPDATE password_reset_tokens SET expires_at = ? WHERE reset_id = ?",
                [(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(), tok2["reset_id"]],
            )
        exp_resp = TestClient(app).post(
            "/api/auth/reset-password/confirm",
            json={"reset_token": tok2["reset_token"], "new_password": "Expired-Reset-Pass!2029"},
        )
        assert exp_resp.status_code == 400

        # Security audit log has no DELETE route & captures key actions
        assert owner_c.delete("/api/admin/audit-logs").status_code == 405
        logs = owner_c.get("/api/admin/audit-logs?limit=100").json()["items"]
        ev_types = {row["event_type"] for row in logs}
        assert {"OWNER_BOOTSTRAP", "USER_CREATED", "PASSWORD_CHANGED", "PASSWORD_RESET_ISSUED", "PASSWORD_RESET_COMPLETED"}.issubset(ev_types)


# ----------------------------------------------------------------------
# 10. Worker Identity Fencing & Permanent Financial Safety Locks
# ----------------------------------------------------------------------


def test_worker_lease_redaction_impersonation_block_and_safety_locks(tmp_path):
    settings = SecuritySettings(env_mode="development", session_secret="w" * 48, rate_limit_enabled=False)
    app = create_app(db_path=str(tmp_path / "worker.duckdb"), enable_background_loop=False, security_settings=settings)
    with TestClient(app) as owner_c:
        authenticate_test_client(owner_c)

        # Lease a task internally so lease_token exists in DB
        sup = app.state.supervisor
        t = sup.submit_task(
            task_type="CHECK_FEED_HEALTH",
            assigned_agent="MARKET_INTELLIGENCE",
            payload={"fencing_check": True},
        )["task"]
        w = sup.spawn_worker("MARKET_INTELLIGENCE")
        leased = sup.claim_next_task(w["worker_id"])
        assert leased is not None and leased["lease_token"].startswith("LEASE-")

        # HTTP task listings redact raw lease_token
        tasks_http = owner_c.get("/api/agents/tasks").json()["items"]
        matched = next(x for x in tasks_http if x["task_id"] == t["task_id"])
        assert matched["lease_token"] == "[INTERNAL_FENCED]"

        cc_http = owner_c.get("/api/intelligence/command-center").json()
        assert leased["lease_token"] not in json.dumps(cc_http)

        # Human caller attempting worker heartbeat without server capability gets 403
        hb_block = owner_c.post(
            f"/api/agents/tasks/{t['task_id']}/heartbeat",
            json={"worker_id": w["worker_id"], "lease_token": leased["lease_token"]},
        )
        assert hb_block.status_code == 403

        # Even OWNER cannot bypass permanent financial locks
        dash = owner_c.get("/api/dashboard").json()
        assert dash["live_trading_enabled"] is False
        assert dash["risk_gate"]["policy"]["live_trading_enabled"] is False
        assert dash["risk_gate"]["policy"]["min_cross_venue_edge_pct"] == 2.0
        assert dash["risk_gate"]["policy"]["max_venue_exposure_usd"]["polymarket"] == 2500.0
        assert dash["risk_gate"]["policy"]["daily_loss_limits_usd"]["tier1"] == 250.0
        mods = {m["module_id"]: m["status"] for m in dash["modules"]}
        assert mods["tier2_delta_neutral"] == "INACTIVE"
        assert mods["tier3_ai_research"] == "INACTIVE"

        live_attempt = owner_c.post(
            "/api/risk-gate/evaluate",
            json={
                "venue": "polymarket",
                "arbitrage_type": "intra_venue",
                "strategy_tier": "tier1",
                "execution_mode": "LIVE",
                "market_id": "LIVE-TRY",
                "requested_notional_usd": 20.0,
                "net_edge_pct": 5.0,
                "liquidity_participation_pct": 1.0,
                "data_age_ms": 100,
            },
        ).json()
        assert live_attempt["approved"] is False
        assert "LIVE_EXECUTION_LOCK" in live_attempt["failed_rules"]


# ----------------------------------------------------------------------
# 11. Disposable Phase 3E -> Stage 4A DuckDB Schema Migration Test
# ----------------------------------------------------------------------


def test_disposable_phase3e_to_stage4a_migration_preserves_existing_data(tmp_path):
    legacy_db = tmp_path / "phase3e_legacy.duckdb"
    # Simulate an existing Phase 3E DuckDB created BEFORE Stage 4A auth tables existed
    conn = duckdb.connect(str(legacy_db))
    conn.execute(
        """
        CREATE TABLE agent_memory (
            memory_id VARCHAR PRIMARY KEY,
            category VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            summary VARCHAR NOT NULL,
            evidence_json VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL
        );
        CREATE TABLE predictions (
            prediction_id VARCHAR PRIMARY KEY,
            agent_id VARCHAR NOT NULL,
            market_id VARCHAR NOT NULL,
            venue VARCHAR NOT NULL,
            subject_key VARCHAR NOT NULL,
            question VARCHAR NOT NULL,
            predicted_prob DOUBLE NOT NULL,
            market_implied_prob DOUBLE,
            confidence DOUBLE NOT NULL,
            rationale VARCHAR NOT NULL,
            evidence_json VARCHAR NOT NULL,
            provenance VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            actual_outcome INTEGER,
            brier_score DOUBLE,
            log_loss DOUBLE,
            learning_cycle_id VARCHAR,
            created_at VARCHAR NOT NULL,
            resolved_at VARCHAR,
            market_category VARCHAR NOT NULL DEFAULT 'general'
        );
        INSERT INTO agent_memory VALUES (
            'MEM-LEGACY-001', 'VERIFIED_LESSON', 'Legacy Rule',
            'Must remain intact after Stage 4A schema migration.', '{}', 'RECORDED', '2026-10-01T00:00:00+00:00'
        );
        INSERT INTO predictions VALUES (
            'PRED-LEGACY-001', 'probability_research_agent', 'M-LEG', 'polymarket',
            'legacy_subj', 'Will legacy data survive?', 0.75, 0.70, 0.80,
            'Historical forecast', '[]', 'SIMULATED', 'OPEN', NULL, NULL, NULL, NULL,
            '2026-10-01T00:00:00+00:00', NULL, 'general'
        );
        """
    )
    conn.close()

    # Open with Stage 4A DuckDBStore & verify additive migration
    store = DuckDBStore(db_path=str(legacy_db))
    try:
        counts = store.get_counts()
        assert counts["memories"] == 1
        assert counts["predictions"] == 1
        assert counts["users"] == 0
        assert counts["active_sessions"] == 0
        assert counts["security_audit_logs"] == 0

        legacy_mem = store._conn.execute(
            "SELECT title FROM agent_memory WHERE memory_id = 'MEM-LEGACY-001'"
        ).fetchone()
        assert legacy_mem == ("Legacy Rule",)
        legacy_pred = store._conn.execute(
            "SELECT predicted_prob FROM predictions WHERE prediction_id = 'PRED-LEGACY-001'"
        ).fetchone()
        assert legacy_pred == (0.75,)
    finally:
        store.close()


# ----------------------------------------------------------------------
# 12. HTTPS Reverse-Proxy Cross-Site Iframe Cookie Policy (CHIPS / SameSite=None)
# ----------------------------------------------------------------------


def test_https_proxy_emits_partitioned_samesite_none_cookies_for_iframe_preview(tmp_path):
    settings = SecuritySettings(
        env_mode="development",
        session_secret="k" * 48,
        cookie_secure=False,
        trusted_proxies=("127.0.0.1", "testclient"),
        rate_limit_enabled=False,
    )
    app = create_app(
        db_path=str(tmp_path / "iframe_cookies.duckdb"),
        enable_background_loop=False,
        security_settings=settings,
    )
    preview_host = "ais-dev-example.us-east1.run.app"
    preview_origin = f"https://{preview_host}"

    # Plain HTTP request preserves SameSite=lax without Secure
    http_client = TestClient(app)
    http_status = http_client.get("/api/auth/status")
    assert http_status.status_code == 200
    assert http_status.json()["cookie_secure"] is False

    # Proxied HTTPS request (e.g. Cloud Run -> Express -> FastAPI) emits Secure; SameSite=none; Partitioned
    with TestClient(app, base_url=preview_origin) as client:
        proxy_headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": preview_host,
            "Origin": preview_origin,
        }
        https_status = client.get("/api/auth/status", headers=proxy_headers)
        assert https_status.status_code == 200
        assert https_status.json()["cookie_secure"] is True

        boot = client.post(
            "/api/auth/bootstrap-owner",
            headers=proxy_headers,
            json={"username": "iframe_owner", "password": OWNER_PW},
        )
        assert boot.status_code == 200, boot.text
        assert boot.json()["csrf_token"]
        set_cookies = boot.headers.get_list("set-cookie")
        sess_hdr = next(h for h in set_cookies if h.startswith("certainty_session="))
        csrf_hdr = next(h for h in set_cookies if h.startswith("certainty_csrf="))
        for hdr in (sess_hdr, csrf_hdr):
            lower = hdr.lower()
            assert "secure" in lower
            assert "samesite=none" in lower
            assert "partitioned" in lower
        assert "httponly" in sess_hdr.lower()
        assert "httponly" not in csrf_hdr.lower()

        # Subsequent authenticated mutation using X-Forwarded-Host Origin + CSRF succeeds
        sync_csrf_header(client)
        mem = client.post(
            "/api/memory",
            headers=proxy_headers,
            json={
                "category": "VERIFIED_LESSON",
                "title": "Iframe proxy session check",
                "summary": "Verify session persists cleanly across HTTPS iframe proxy.",
            },
        )
        assert mem.status_code == 200, mem.text

        # Default login does NOT expose bearer_token in JSON
        normal_login = client.post(
            "/api/auth/login",
            headers=proxy_headers,
            json={"username": "iframe_owner", "password": OWNER_PW},
        )
        assert normal_login.status_code == 200
        assert "bearer_token" not in normal_login.json()

        # Cookie-blocked iframe client uses issue_bearer_fallback=True and sends no cookies
        fb_login = client.post(
            "/api/auth/login",
            headers=proxy_headers,
            json={"username": "iframe_owner", "password": OWNER_PW, "issue_bearer_fallback": True},
        )
        assert fb_login.status_code == 200
        bearer_tok = fb_login.json()["bearer_token"]
        fb_csrf = fb_login.json()["csrf_token"]
        assert len(bearer_tok) >= 16

        cookieless = TestClient(app, base_url=preview_origin)
        cookieless.cookies.clear()
        auth_st = cookieless.get(
            "/api/auth/status",
            headers={**proxy_headers, "Authorization": f"Bearer {bearer_tok}"},
        )
        assert auth_st.status_code == 200
        assert auth_st.json()["authenticated"] is True

        # Unsafe POST without X-CSRF-Token still fails closed 403
        no_csrf_fb = cookieless.post(
            "/api/memory",
            headers={**proxy_headers, "Authorization": f"Bearer {bearer_tok}"},
            json={"category": "TEST", "title": "No CSRF", "summary": "Should 403"},
        )
        assert no_csrf_fb.status_code == 403

        # With X-CSRF-Token + Origin + Bearer, mutation succeeds and logout revokes
        ok_fb = cookieless.post(
            "/api/memory",
            headers={
                **proxy_headers,
                "Authorization": f"Bearer {bearer_tok}",
                "X-CSRF-Token": fb_csrf,
            },
            json={
                "category": "VERIFIED_LESSON",
                "title": "Cookie-blocked iframe session",
                "summary": "Bearer + CSRF fallback verified cleanly.",
            },
        )
        assert ok_fb.status_code == 200

        out_fb = cookieless.post(
            "/api/auth/logout",
            headers={
                **proxy_headers,
                "Authorization": f"Bearer {bearer_tok}",
                "X-CSRF-Token": fb_csrf,
            },
        )
        assert out_fb.status_code == 200
        after_out = cookieless.get(
            "/api/auth/status",
            headers={**proxy_headers, "Authorization": f"Bearer {bearer_tok}"},
        )
        assert after_out.json()["authenticated"] is False

