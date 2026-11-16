"""FastAPI server for The Certainty Stack (Phase 1-4A — Authenticated, RBAC-Protected Research & Paper Sim)."""

from __future__ import annotations

import hmac
import os
import re
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.arbitrage.cross_venue import CrossVenueScanner
from backend.arbitrage.matcher import assess_contract_equivalence, title_similarity_score
from backend.arbitrage.pair_discovery import discover_pairs
from backend.arbitrage.pair_review import (
    build_pair_review,
    build_rejected_record,
    pair_review_state,
    prioritize_pairs_for_scan,
)
from backend.arbitrage.scanner import PolymarketArbitrageScanner
from backend.execution.paper_simulator import PaperExecutionEngine, PaperExecutionRequest
from backend.future.modules import get_module_registry
from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import MemoryTier, StructuredMemoryManager
from backend.intelligence.model_router import CostAwareModelRouter
from backend.intelligence.operations import OperationalHealthMonitor, SoakMode, SoakTestManager
from backend.intelligence.probability import ConfigGovernanceManager, ProbabilityIntelligenceEngine
from backend.intelligence.supervisor import SupervisorOrchestrator
from backend.intelligence.validation_labs import (
    AdversarialReviewEngine,
    ConcurrencyStressLab,
    PaperStressLab,
    ResearchQualityValidator,
)
from backend.risk_gate.engine import (
    DeterministicRiskGate,
    RiskEvaluationRequest,
    RiskEvaluationResult,
)
from backend.risk_gate.policy import RiskPolicy
from backend.security import (
    CSRF_HEADER_NAME,
    AuthManager,
    AuthenticatedUser,
    DuckDBRateLimiter,
    SecuritySettings,
    load_security_settings,
    validate_security_settings,
)
from backend.security.config import SESSION_COOKIE_NAME, resolve_client_ip
from backend.security.dependencies import (
    clear_auth_cookies,
    enforce_rate_limit_check,
    enforce_tls_and_origin,
    make_guard,
    optional_authenticated_user,
    set_auth_cookies,
)
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import KalshiReadOnlyClient
from backend.venues.polymarket import PolymarketReadOnlyClient


def sanitize_error_message(raw: Any) -> str:
    """Redact any environment secret values or credential patterns from error strings."""
    text = str(raw or "")
    for env_name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "KALSHI_API_KEY",
        "POLYMARKET_API_KEY",
        "PRIVATE_KEY",
        "WALLET_PRIVATE_KEY",
        "CERTAINTY_SESSION_SECRET",
        "CERTAINTY_BOOTSTRAP_TOKEN",
    ):
        secret = os.environ.get(env_name, "").strip()
        if len(secret) >= 6:
            text = text.replace(secret, "[REDACTED_SECRET]")
    text = re.sub(r"AIza[0-9A-Za-z_-]+", "[REDACTED_KEY]", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(api[_-]?key|token|secret|password|cookie)\s*=\s*\S+",
        r"\1=[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _redact_tasks_for_http(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never expose raw Supervisor fencing lease tokens over human-facing HTTP endpoints."""
    sanitized: list[dict[str, Any]] = []
    for t in tasks:
        copy = dict(t)
        if copy.get("lease_token"):
            copy["lease_token"] = "[INTERNAL_FENCED]"
        sanitized.append(copy)
    return sanitized


def _redact_supervisor_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    out = dict(snapshot)
    if "recent_activity" in out:
        # Activity summaries may include lease tokens when task leased internally; mask them
        cleaned_act: list[dict[str, Any]] = []
        for act in out["recent_activity"]:
            item = dict(act)
            item["summary"] = re.sub(r"LEASE-[A-Za-z0-9]+", "LEASE-[FENCED]", str(item.get("summary", "")))
            cleaned_act.append(item)
        out["recent_activity"] = cleaned_act
    return out


# ----------------------------------------------------------------------
# Request Models (Auth, Admin & Domain)
# ----------------------------------------------------------------------


class BootstrapOwnerRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)
    email: str | None = Field(default=None, max_length=120)
    bootstrap_token: str | None = Field(default=None, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class ResetPasswordConfirmRequest(BaseModel):
    reset_token: str = Field(min_length=10, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)


class AdminCreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)
    role: str = Field(min_length=3, max_length=24)
    email: str | None = Field(default=None, max_length=120)
    must_rotate_password: bool = False


class AdminUpdateRoleRequest(BaseModel):
    role: str = Field(min_length=3, max_length=24)


class AdminResetTokenRequest(BaseModel):
    ttl_minutes: int = Field(default=30, ge=5, le=120)


class SessionRevokeRequest(BaseModel):
    reason: str = Field(default="Revoked by authorized principal", min_length=3, max_length=200)


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


class CrossVenueDiscoverRequest(BaseModel):
    persist: bool = False  # False = dry-run preview; True = persist UNVERIFIED candidates
    poly_pages: int = Field(default=5, ge=1, le=10)
    kalshi_max_pages: int = Field(default=40, ge=1, le=60)
    min_similarity: float = Field(default=0.3, ge=0.1, le=1.0)


class CrossVenuePairRejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=400)


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


class AgentTaskCreateRequest(BaseModel):
    task_type: str = Field(min_length=2, max_length=64)
    assigned_agent: str = Field(min_length=2, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=5, ge=1, le=10)
    depends_on: list[str] = Field(default_factory=list)
    dedup_key: str | None = Field(default=None, max_length=140)
    timeout_seconds: float = Field(default=30.0, gt=0.05, le=300.0)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    max_recovery_attempts: int | None = Field(default=None, ge=0, le=5)
    created_by: str = Field(default="operator", min_length=2, max_length=64)


class TaskHeartbeatRequest(BaseModel):
    worker_id: str = Field(min_length=2, max_length=64)
    lease_token: str = Field(min_length=2, max_length=64)
    extend_lease_seconds: float | None = Field(default=None, ge=1.0, le=120.0)


class AgentDispatchRequest(BaseModel):
    max_tasks: int = Field(default=4, ge=1, le=8)


class WorkerScaleRequest(BaseModel):
    target_concurrency: int | None = Field(default=None, ge=0, le=8)
    max_concurrency: int | None = Field(default=None, ge=1, le=8)


class BackgroundLoopStartRequest(BaseModel):
    interval_seconds: float = Field(default=5.0, ge=0.05, le=60.0)
    max_tasks_per_tick: int = Field(default=4, ge=1, le=8)


class PredictionCreateRequest(BaseModel):
    agent_id: str = Field(min_length=2, max_length=64)
    market_id: str = Field(min_length=1, max_length=128)
    venue: str = Field(default="polymarket", min_length=2, max_length=32)
    subject_key: str = Field(min_length=2, max_length=120)
    question: str = Field(min_length=3, max_length=300)
    predicted_prob: float = Field(ge=0.0, le=1.0)
    market_implied_prob: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=600)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    provenance: str = Field(default="SIMULATED", max_length=24)
    market_category: str = Field(default="general", min_length=1, max_length=64)


class PredictionResolveRequest(BaseModel):
    outcome: int | str
    resolution_source: str = Field(default="confirmed_settlement", min_length=2, max_length=120)
    reviewer_id: str = Field(default="calibration_auditor", min_length=2, max_length=64)


class ModelRouteRequest(BaseModel):
    task_id: str | None = Field(default=None, max_length=64)
    agent_id: str = Field(min_length=2, max_length=64)
    task_category: str = Field(min_length=2, max_length=64)
    prefer_ai: bool = False
    estimated_input_tokens: int = Field(default=800, ge=0, le=500_000)
    estimated_output_tokens: int = Field(default=300, ge=0, le=100_000)
    estimated_cost_override_usd: float | None = Field(default=None, ge=0.0, le=100.0)


class ConfigProposeRequest(BaseModel):
    config_domain: str = Field(min_length=2, max_length=64)
    proposed_config: dict[str, Any] = Field(default_factory=dict)
    proposed_by: str = Field(min_length=2, max_length=64)
    change_reason: str = Field(min_length=3, max_length=300)
    source_cycle_id: str | None = Field(default=None, max_length=64)
    regression_case_id: str | None = Field(default=None, max_length=64)


class ConfigActivateRequest(BaseModel):
    reviewed_by: str = Field(min_length=2, max_length=64)


class ConfigRollbackRequest(BaseModel):
    config_domain: str = Field(min_length=2, max_length=64)
    target_version: int = Field(ge=1)
    actor: str = Field(default="operator", min_length=2, max_length=64)
    reason: str = Field(default="Operator configuration rollback", min_length=3, max_length=240)


class SoakStartRequest(BaseModel):
    mode: SoakMode = "SHORT_VALIDATION"
    duration_seconds: float | None = Field(default=None, ge=0.2, le=259200.0)
    tick_interval_seconds: float = Field(default=2.0, ge=0.1, le=60.0)


class SoakStopRequest(BaseModel):
    run_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(default="Stopped manually before target duration completed", max_length=240)


class ConcurrencyStressRequest(BaseModel):
    task_count: int = Field(default=12, ge=4, le=32)
    worker_count: int = Field(default=4, ge=2, le=8)


class ResearchValidationRequest(BaseModel):
    agent_id: str = Field(default="market_intelligence_agent", min_length=2, max_length=64)
    source_identifier: str = Field(default="", max_length=160)
    source_timestamp: str | None = None
    retrieval_timestamp: str | None = None
    evidence_summary: str = Field(default="", max_length=1200)
    market_id: str = Field(default="", max_length=128)
    market_question: str = Field(default="", max_length=400)
    raw_external_text: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    conflicting_claims: list[dict[str, Any]] = Field(default_factory=list)
    cross_venue_check: dict[str, Any] | None = None
    max_age_ms: int = Field(default=15000, ge=100, le=600000)


class PaperStressRequest(BaseModel):
    scenarios: list[str] | None = None
    target_shares: float = Field(default=25.0, gt=0.0, le=500.0)


class AdversarialReviewRequest(BaseModel):
    subject_key: str = Field(min_length=2, max_length=120)
    conclusion_agent: str = Field(min_length=2, max_length=64)
    conclusion_summary: str = Field(min_length=3, max_length=600)
    proposer_evidence: list[dict[str, Any]] = Field(default_factory=list)
    challenger_agent: str | None = Field(default=None, max_length=64)
    arbiter_agent: str | None = Field(default=None, max_length=64)
    metrics: dict[str, Any] = Field(default_factory=dict)
    opposing_evidence: list[dict[str, Any]] = Field(default_factory=list)


def create_app(
    db_path: str | None = None,
    *,
    enable_background_loop: bool = True,
    loop_interval_seconds: float = 5.0,
    security_settings: SecuritySettings | None = None,
) -> FastAPI:
    sec_settings = security_settings or load_security_settings()
    validate_security_settings(sec_settings)

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
    prob_engine = ProbabilityIntelligenceEngine(store=store, learning=learning_engine)
    config_manager = ConfigGovernanceManager(store=store, learning=learning_engine)
    model_router = CostAwareModelRouter(store=store)
    supervisor = SupervisorOrchestrator(
        store=store,
        risk_gate=risk_gate,
        memory=memory_manager,
        learning=learning_engine,
        scanner=scanner,
        cross_scanner=cross_scanner,
        model_router=model_router,
    )
    health_monitor = OperationalHealthMonitor(
        store=store, supervisor=supervisor, model_router=model_router
    )
    soak_manager = SoakTestManager(
        store=store,
        supervisor=supervisor,
        health_monitor=health_monitor,
        model_router=model_router,
    )
    supervisor.register_tick_hook(soak_manager.tick_active_soak)
    concurrency_lab = ConcurrencyStressLab(store=store, learning=learning_engine)
    research_validator = ResearchQualityValidator(store=store, learning=learning_engine)
    paper_stress_lab = PaperStressLab(store=store, learning=learning_engine)
    adversarial_engine = AdversarialReviewEngine(
        store=store, memory=memory_manager, learning=learning_engine
    )
    auth_manager = AuthManager(store=store, settings=sec_settings)
    rate_limiter = DuckDBRateLimiter(
        store=store,
        enabled=sec_settings.rate_limit_enabled,
        overrides=sec_settings.rate_limit_overrides,
    )
    # Ephemeral internal worker capability key (never exposed over HTTP)
    internal_worker_key = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if enable_background_loop and os.environ.get("CERTAINTY_DISABLE_SUPERVISOR_LOOP") != "1":
            await supervisor.start_background_loop(interval_seconds=loop_interval_seconds)
        try:
            yield
        finally:
            await supervisor.stop_background_loop()
            store.close()

    app = FastAPI(
        title="The Certainty Stack API",
        version="0.4.0-stage4a",
        description="Authenticated, RBAC-protected Fail-Closed Risk Gate, Polymarket + Kalshi read-only ingestion, cross-venue paper simulator, structured memory & learning core, multi-agent Supervisor, continuous operations monitor, validation labs, and DuckDB analytics.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(sec_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", CSRF_HEADER_NAME],
    )

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith(("/api/auth", "/api/admin")):
            response.headers["Cache-Control"] = "no-store"
        if sec_settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response

    app.state.store = store
    app.state.risk_gate = risk_gate
    app.state.scanner = scanner
    app.state.cross_scanner = cross_scanner
    app.state.paper_engine = paper_engine
    app.state.memory_manager = memory_manager
    app.state.learning_engine = learning_engine
    app.state.prob_engine = prob_engine
    app.state.config_manager = config_manager
    app.state.model_router = model_router
    app.state.supervisor = supervisor
    app.state.health_monitor = health_monitor
    app.state.soak_manager = soak_manager
    app.state.concurrency_lab = concurrency_lab
    app.state.research_validator = research_validator
    app.state.paper_stress_lab = paper_stress_lab
    app.state.adversarial_engine = adversarial_engine
    app.state.poly_client = poly_client
    app.state.kalshi_client = kalshi_client
    app.state.security_settings = sec_settings
    app.state.auth_manager = auth_manager
    app.state.rate_limiter = rate_limiter
    app.state._internal_worker_key = internal_worker_key

    # Deny-by-default RBAC route guards
    read_only_guard = make_guard("READ_ONLY")
    analyst_guard = make_guard("ANALYST")
    operator_guard = make_guard("OPERATOR")
    operator_dispatch_guard = make_guard("OPERATOR", rate_limit_class="AGENT_DISPATCH")
    operator_paper_guard = make_guard("OPERATOR", rate_limit_class="PAPER_EXECUTE")
    operator_model_guard = make_guard("OPERATOR", rate_limit_class="MODEL_ROUTE")
    operator_lab_guard = make_guard("OPERATOR", rate_limit_class="VALIDATION_LAB")
    owner_guard = make_guard("OWNER")
    owner_config_guard = make_guard("OWNER", rate_limit_class="CONFIG_CHANGE")
    owner_kill_switch_guard = make_guard("OWNER", rate_limit_class="KILL_SWITCH")
    owner_admin_guard = make_guard("OWNER", rate_limit_class="ADMIN_WRITE")
    owner_reset_guard = make_guard("OWNER", rate_limit_class="PASSWORD_RESET")

    # ------------------------------------------------------------------
    # Stage 4A: Authentication & Session Endpoints
    # ------------------------------------------------------------------
    @app.get("/api/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        enforce_tls_and_origin(
            request,
            settings=sec_settings,
            auth_manager=auth_manager,
            require_origin_for_unsafe=False,
        )
        user = optional_authenticated_user(request)
        return {
            "authenticated": user is not None,
            "needs_owner_bootstrap": auth_manager.needs_owner_bootstrap(),
            "env_mode": sec_settings.env_mode,
            "cookie_secure": sec_settings.cookie_secure,
            "tls_required": sec_settings.is_production and sec_settings.require_tls_in_production,
            "csrf_header_name": CSRF_HEADER_NAME,
            "user": user.to_public_dict() if user else None,
        }

    @app.post("/api/auth/bootstrap-owner")
    async def bootstrap_owner(
        req: BootstrapOwnerRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        enforce_tls_and_origin(request, settings=sec_settings, auth_manager=auth_manager)
        client_ip = resolve_client_ip(request, sec_settings)
        enforce_rate_limit_check(
            request,
            rate_limiter=rate_limiter,
            auth_manager=auth_manager,
            settings=sec_settings,
            bucket_class="BOOTSTRAP",
            identifier=f"ip:{client_ip}",
        )
        try:
            owner_rec = auth_manager.bootstrap_owner(
                username=req.username,
                password=req.password,
                email=req.email,
                bootstrap_token=req.bootstrap_token,
                ip_address=client_ip,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        sess = auth_manager.create_session_after_bootstrap(
            user_record=owner_rec,
            ip_address=client_ip,
            user_agent=request.headers.get("user-agent", "unknown"),
        )
        set_auth_cookies(
            response,
            session_token=sess["session_token"],
            csrf_token=sess["csrf_token"],
            settings=sec_settings,
        )
        return {
            "enrolled": True,
            "user": sess["user"],
            "expires_at": sess["expires_at"],
        }

    @app.post("/api/auth/login")
    async def login(
        req: LoginRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        enforce_tls_and_origin(request, settings=sec_settings, auth_manager=auth_manager)
        client_ip = resolve_client_ip(request, sec_settings)
        enforce_rate_limit_check(
            request,
            rate_limiter=rate_limiter,
            auth_manager=auth_manager,
            settings=sec_settings,
            bucket_class="LOGIN_IP",
            identifier=f"ip:{client_ip}",
        )
        enforce_rate_limit_check(
            request,
            rate_limiter=rate_limiter,
            auth_manager=auth_manager,
            settings=sec_settings,
            bucket_class="LOGIN_USER",
            identifier=f"uname:{req.username.strip().lower()}",
        )
        prior_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        try:
            sess = auth_manager.login(
                username=req.username,
                password=req.password,
                ip_address=client_ip,
                user_agent=request.headers.get("user-agent", "unknown"),
                prior_session_token=prior_cookie,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        set_auth_cookies(
            response,
            session_token=sess["session_token"],
            csrf_token=sess["csrf_token"],
            settings=sec_settings,
        )
        return {
            "authenticated": True,
            "user": sess["user"],
            "expires_at": sess["expires_at"],
        }

    @app.post("/api/auth/logout")
    async def logout(
        request: Request,
        response: Response,
        user: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        if raw_token:
            auth_manager.revoke_session_by_token(raw_token, reason="User logged out")
        auth_manager.record_audit_event(
            event_type="LOGOUT",
            outcome="SUCCESS",
            ip_address=client_ip,
            endpoint="/api/auth/logout",
            method="POST",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            detail={"session_id": user.session_id},
        )
        clear_auth_cookies(response, settings=sec_settings)
        return {"logged_out": True}

    @app.get("/api/auth/me")
    async def auth_me(user: AuthenticatedUser = Depends(read_only_guard)) -> dict[str, Any]:
        return {"user": user.to_public_dict()}

    @app.post("/api/auth/change-password")
    async def change_password(
        req: ChangePasswordRequest,
        request: Request,
        response: Response,
        user: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            sess = auth_manager.change_password(
                actor=user,
                current_password=req.current_password,
                new_password=req.new_password,
                ip_address=client_ip,
                user_agent=request.headers.get("user-agent", "unknown"),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        set_auth_cookies(
            response,
            session_token=sess["session_token"],
            csrf_token=sess["csrf_token"],
            settings=sec_settings,
        )
        return {
            "password_changed": True,
            "user": sess["user"],
            "expires_at": sess["expires_at"],
        }

    @app.post("/api/auth/reset-password/confirm")
    async def confirm_password_reset(
        req: ResetPasswordConfirmRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        enforce_tls_and_origin(request, settings=sec_settings, auth_manager=auth_manager)
        client_ip = resolve_client_ip(request, sec_settings)
        enforce_rate_limit_check(
            request,
            rate_limiter=rate_limiter,
            auth_manager=auth_manager,
            settings=sec_settings,
            bucket_class="PASSWORD_RESET",
            identifier=f"ip:{client_ip}",
        )
        try:
            res = auth_manager.confirm_password_reset(
                reset_token=req.reset_token,
                new_password=req.new_password,
                ip_address=client_ip,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        clear_auth_cookies(response, settings=sec_settings)
        return res

    @app.get("/api/auth/sessions")
    async def list_own_sessions(
        user: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = auth_manager.list_sessions(user_id=user.user_id, include_revoked=False, limit=50)
        return {"count": len(items), "items": items}

    @app.post("/api/auth/sessions/{session_id}/revoke")
    async def revoke_own_session(
        session_id: str,
        req: SessionRevokeRequest,
        request: Request,
        response: Response,
        user: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            res = auth_manager.revoke_session_by_id(
                session_id, reason=req.reason, actor=user, ip_address=client_ip
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session_id == user.session_id:
            clear_auth_cookies(response, settings=sec_settings)
        return res

    # ------------------------------------------------------------------
    # Stage 4A: OWNER-Only User, Role, Session & Security Audit Endpoints
    # ------------------------------------------------------------------
    @app.get("/api/admin/users")
    async def admin_list_users(_: AuthenticatedUser = Depends(owner_guard)) -> dict[str, Any]:
        items = auth_manager.list_users()
        return {"count": len(items), "items": items}

    @app.post("/api/admin/users")
    async def admin_create_user(
        req: AdminCreateUserRequest,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_admin_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            created = auth_manager.create_user(
                username=req.username,
                password=req.password,
                role=req.role,
                email=req.email,
                must_rotate_password=req.must_rotate_password,
                actor_username=owner.username,
                actor_user_id=owner.user_id,
                ip_address=client_ip,
            )
            return {"user": created}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/admin/users/{user_id}/role")
    async def admin_update_user_role(
        user_id: str,
        req: AdminUpdateRoleRequest,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_admin_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            updated = auth_manager.update_user_role(
                user_id,
                new_role=req.role,
                actor_username=owner.username,
                actor_user_id=owner.user_id,
                ip_address=client_ip,
            )
            return {"user": updated}
        except ValueError as exc:
            msg = str(exc)
            code = 404 if "Unknown user_id" in msg else 400
            raise HTTPException(status_code=code, detail=msg) from exc

    @app.post("/api/admin/users/{user_id}/deactivate")
    async def admin_deactivate_user(
        user_id: str,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_admin_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            updated = auth_manager.set_user_active(
                user_id,
                is_active=False,
                actor_username=owner.username,
                actor_user_id=owner.user_id,
                ip_address=client_ip,
            )
            return {"user": updated}
        except ValueError as exc:
            msg = str(exc)
            code = 404 if "Unknown user_id" in msg else 400
            raise HTTPException(status_code=code, detail=msg) from exc

    @app.post("/api/admin/users/{user_id}/reactivate")
    async def admin_reactivate_user(
        user_id: str,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_admin_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            updated = auth_manager.set_user_active(
                user_id,
                is_active=True,
                actor_username=owner.username,
                actor_user_id=owner.user_id,
                ip_address=client_ip,
            )
            return {"user": updated}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/admin/users/{user_id}/reset-token")
    async def admin_issue_reset_token(
        user_id: str,
        req: AdminResetTokenRequest,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_reset_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            return auth_manager.issue_password_reset_token(
                target_user_id=user_id,
                actor=owner,
                ttl_minutes=req.ttl_minutes,
                ip_address=client_ip,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/admin/sessions")
    async def admin_list_sessions(
        include_revoked: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=300),
        _: AuthenticatedUser = Depends(owner_guard),
    ) -> dict[str, Any]:
        items = auth_manager.list_sessions(include_revoked=include_revoked, limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/admin/sessions/{session_id}/revoke")
    async def admin_revoke_session(
        session_id: str,
        req: SessionRevokeRequest,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_admin_guard),
    ) -> dict[str, Any]:
        client_ip = resolve_client_ip(request, sec_settings)
        try:
            return auth_manager.revoke_session_by_id(
                session_id,
                reason=req.reason,
                actor=owner,
                ip_address=client_ip,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/admin/audit-logs")
    async def admin_list_audit_logs(
        event_type: str | None = Query(default=None),
        outcome: str | None = Query(default=None),
        limit: int = Query(default=120, ge=1, le=500),
        _: AuthenticatedUser = Depends(owner_guard),
    ) -> dict[str, Any]:
        items = auth_manager.list_audit_logs(
            event_type=event_type, outcome=outcome, limit=limit
        )
        return {"count": len(items), "immutable": True, "items": items}

    # ------------------------------------------------------------------
    # Health (Minimal Public Liveness; Full Telemetry Authenticated Only)
    # ------------------------------------------------------------------
    @app.get("/api/health")
    async def health(
        request: Request,
        probe_venues: bool = Query(default=True),
    ) -> dict[str, Any]:
        enforce_tls_and_origin(
            request,
            settings=sec_settings,
            auth_manager=auth_manager,
            require_origin_for_unsafe=False,
        )
        user = optional_authenticated_user(request)
        if user is None:
            return {
                "service": "the-certainty-stack",
                "status": "NOMINAL",
                "auth_required": True,
                "authenticated": False,
                "needs_owner_bootstrap": auth_manager.needs_owner_bootstrap(),
                "live_trading_enabled": False,
                "checked_at": utc_now_iso(),
            }

        counts = store.get_counts()
        kill_switch = store.get_state(
            "kill_switch_active", {"active": False, "reason": "Default inactive"}
        )
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
            "phase": "Phase 4A — Authenticated Research & Cross-Venue Paper Execution",
            "status": "NOMINAL",
            "auth_required": True,
            "authenticated": True,
            "user_role": user.role,
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

    # ------------------------------------------------------------------
    # Core Dashboard, Market Discovery & Risk Gate Routes
    # ------------------------------------------------------------------
    @app.get("/api/dashboard")
    async def dashboard(_: AuthenticatedUser = Depends(read_only_guard)) -> dict[str, Any]:
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
            "phase": "Phase 4A",
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
    async def scan_polymarket(
        req: ScanRequest,
        _: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        return await scanner.scan(
            limit=req.limit,
            target_shares=req.target_shares,
            slippage_buffer_bps=req.slippage_buffer_bps,
        )

    @app.get("/api/markets/{market_id}/book")
    async def market_book(
        market_id: str,
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
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
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = store.list_opportunities(limit=limit, latest_scan_only=latest_scan_only)
        return {"count": len(items), "items": items}

    @app.post("/api/risk-gate/evaluate", response_model=RiskEvaluationResult)
    async def evaluate_risk_gate(
        req: RiskEvaluationRequest,
        _: AuthenticatedUser = Depends(analyst_guard),
    ) -> RiskEvaluationResult:
        return risk_gate.evaluate(req)

    @app.post("/api/risk-gate/kill-switch")
    async def toggle_kill_switch(
        req: KillSwitchRequest,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_kill_switch_guard),
    ) -> dict[str, Any]:
        # Server-derived actor identity (ignores any caller-asserted req.actor)
        actor_identity = f"{owner.username} ({owner.role})"
        state = store.set_kill_switch(active=req.active, reason=req.reason, actor=actor_identity)
        store.add_memory(
            category="RISK_GOVERNANCE",
            title=f"Global Kill Switch {'ACTIVATED' if req.active else 'DEACTIVATED'}",
            summary=f"Principal '{actor_identity}' set kill_switch={req.active}. Reason: {req.reason}",
            evidence=state,
            status="ENFORCED",
        )
        auth_manager.record_audit_event(
            event_type="KILL_SWITCH_TOGGLED",
            outcome="SUCCESS",
            ip_address=resolve_client_ip(request, sec_settings),
            endpoint="/api/risk-gate/kill-switch",
            method="POST",
            user_id=owner.user_id,
            username=owner.username,
            role=owner.role,
            detail={"active": req.active, "reason": req.reason},
        )
        return state

    @app.get("/api/risk-gate/audit")
    async def risk_audit(
        limit: int = Query(default=100, ge=1, le=500),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = store.list_risk_audit(limit=limit)
        return {"count": len(items), "items": items}

    # ------------------------------------------------------------------
    # Memory & Learning Core Endpoints
    # ------------------------------------------------------------------
    @app.get("/api/memory")
    async def list_memory(
        limit: int = Query(default=50, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = store.list_memory(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/memory")
    async def create_memory(
        req: MemoryCreateRequest,
        user: AuthenticatedUser = Depends(analyst_guard),
    ) -> dict[str, Any]:
        return store.add_memory(
            category=req.category,
            title=req.title,
            summary=req.summary,
            evidence={"recorded_by": user.username, "role": user.role},
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
        _: AuthenticatedUser = Depends(read_only_guard),
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
    async def create_structured_memory(
        req: StructuredMemoryCreateRequest,
        user: AuthenticatedUser = Depends(analyst_guard),
    ) -> dict[str, Any]:
        # Derive human attribution from authenticated session (never trust caller-asserted identity)
        attributed_source = f"user:{user.username}"
        try:
            return memory_manager.record_candidate(
                memory_tier=req.memory_tier,
                category=req.category,
                title=req.title,
                summary=req.summary,
                source_agent=attributed_source,
                content=req.content,
                evidence=req.evidence,
                tags=req.tags,
                confidence=req.confidence,
                explicit_key=req.explicit_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=sanitize_error_message(exc)) from exc

    @app.get("/api/memory/structured/{memory_id}/versions")
    async def get_structured_memory_versions(
        memory_id: str,
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        entry = memory_manager.get_entry(memory_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Unknown memory_id '{memory_id}'")
        versions = memory_manager.get_versions(memory_id)
        return {"memory_id": memory_id, "count": len(versions), "versions": versions}

    @app.post("/api/memory/structured/{memory_id}/rollback")
    async def rollback_structured_memory(
        memory_id: str,
        req: MemoryRollbackRequest,
        user: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        try:
            restored = memory_manager.recover_version(
                memory_id,
                target_version=req.target_version,
                changed_by=user.username,
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
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = learning_engine.list_cycles(stage=stage, source_type=source_type, limit=limit)
        return {"count": len(items), "items": items}

    @app.get("/api/learning/regression-cases")
    async def list_learning_regression_cases(
        limit: int = Query(default=50, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = learning_engine.list_regression_cases(limit=limit)
        return {"count": len(items), "items": items}

    # ------------------------------------------------------------------
    # Stage 3C: Supervisor, Specialized Agents, Worker Pool & Blocker Recovery
    # ------------------------------------------------------------------
    @app.get("/api/agents/status")
    async def agents_status(_: AuthenticatedUser = Depends(read_only_guard)) -> dict[str, Any]:
        return _redact_supervisor_snapshot(supervisor.get_status_snapshot())

    @app.get("/api/agents/tasks")
    async def list_agent_tasks(
        status: str | None = Query(default=None),
        assigned_agent: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=300),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = _redact_tasks_for_http(
            supervisor.list_tasks(status=status, assigned_agent=assigned_agent, limit=limit)
        )
        return {"count": len(items), "items": items}

    @app.post("/api/agents/tasks")
    async def create_agent_task(
        req: AgentTaskCreateRequest,
        user: AuthenticatedUser = Depends(operator_dispatch_guard),
    ) -> dict[str, Any]:
        try:
            res = supervisor.submit_task(
                task_type=req.task_type,
                assigned_agent=req.assigned_agent,
                payload=req.payload,
                priority=req.priority,
                depends_on=req.depends_on,
                dedup_key=req.dedup_key,
                timeout_seconds=req.timeout_seconds,
                max_retries=req.max_retries,
                max_recovery_attempts=req.max_recovery_attempts,
                created_by=user.username,
            )
            if "task" in res and isinstance(res["task"], dict):
                res["task"] = _redact_tasks_for_http([res["task"]])[0]
            return res
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/agents/tasks/{task_id}/heartbeat")
    async def heartbeat_agent_task(
        task_id: str,
        req: TaskHeartbeatRequest,
        request: Request,
        user: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        # Enforce strict separation between human users and internal Supervisor workers
        worker_cap = request.headers.get("x-worker-capability", "")
        if not hmac.compare_digest(worker_cap, app.state._internal_worker_key):
            auth_manager.record_audit_event(
                event_type="WORKER_IMPERSONATION_BLOCKED",
                outcome="DENIED",
                ip_address=resolve_client_ip(request, sec_settings),
                endpoint=request.url.path,
                method="POST",
                user_id=user.user_id,
                username=user.username,
                role=user.role,
                detail={"task_id": task_id, "claimed_worker_id": req.worker_id},
            )
            raise HTTPException(
                status_code=403,
                detail="Human sessions cannot impersonate Supervisor workers.",
            )
        try:
            updated = supervisor.heartbeat(
                task_id,
                worker_id=req.worker_id,
                lease_token=req.lease_token,
                extend_lease_seconds=req.extend_lease_seconds,
            )
            return {"task": _redact_tasks_for_http([updated])[0]}
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agents/dispatch")
    async def dispatch_agent_tasks(
        req: AgentDispatchRequest,
        request: Request,
        user: AuthenticatedUser = Depends(operator_dispatch_guard),
    ) -> dict[str, Any]:
        auth_manager.record_audit_event(
            event_type="AGENT_DISPATCH",
            outcome="SUCCESS",
            ip_address=resolve_client_ip(request, sec_settings),
            endpoint="/api/agents/dispatch",
            method="POST",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            detail={"max_tasks": req.max_tasks},
        )
        return await supervisor.dispatch_cycle(max_tasks=req.max_tasks)

    @app.post("/api/agents/scale")
    async def scale_agent_workers(
        req: WorkerScaleRequest,
        _: AuthenticatedUser = Depends(operator_dispatch_guard),
    ) -> dict[str, Any]:
        if req.max_concurrency is not None:
            supervisor.set_max_concurrency(req.max_concurrency)
        return supervisor.scale_workers(target_concurrency=req.target_concurrency)

    @app.get("/api/agents/blockers")
    async def list_agent_blockers(
        limit: int = Query(default=50, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = supervisor.blocker_engine.list_blockers(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/agents/loop/start")
    async def start_agent_loop(
        req: BackgroundLoopStartRequest,
        _: AuthenticatedUser = Depends(operator_dispatch_guard),
    ) -> dict[str, Any]:
        return await supervisor.start_background_loop(
            interval_seconds=req.interval_seconds,
            max_tasks_per_tick=req.max_tasks_per_tick,
        )

    @app.post("/api/agents/loop/stop")
    async def stop_agent_loop(
        _: AuthenticatedUser = Depends(operator_dispatch_guard),
    ) -> dict[str, Any]:
        return await supervisor.stop_background_loop()

    # ------------------------------------------------------------------
    # Stage 3D: Probability Intelligence, Model Routing, Config Governance & Command Center
    # ------------------------------------------------------------------
    @app.get("/api/intelligence/predictions")
    async def list_predictions(
        status: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        limit: int = Query(default=80, ge=1, le=300),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = prob_engine.list_predictions(status=status, agent_id=agent_id, limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/intelligence/predictions")
    async def create_prediction(
        req: PredictionCreateRequest,
        _: AuthenticatedUser = Depends(analyst_guard),
    ) -> dict[str, Any]:
        try:
            return prob_engine.record_prediction(
                agent_id=req.agent_id,
                market_id=req.market_id,
                venue=req.venue,
                subject_key=req.subject_key,
                question=req.question,
                predicted_prob=req.predicted_prob,
                market_implied_prob=req.market_implied_prob,
                confidence=req.confidence,
                rationale=req.rationale,
                evidence=req.evidence,
                provenance=req.provenance,
                market_category=req.market_category,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=sanitize_error_message(exc)) from exc

    @app.post("/api/intelligence/predictions/{prediction_id}/resolve")
    async def resolve_prediction(
        prediction_id: str,
        req: PredictionResolveRequest,
        user: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        try:
            return prob_engine.resolve_prediction(
                prediction_id,
                outcome=req.outcome,
                resolution_source=req.resolution_source,
                reviewer_id=user.username,
            )
        except ValueError as exc:
            msg = str(exc)
            code = 404 if "Unknown prediction_id" in msg else 400
            raise HTTPException(status_code=code, detail=msg) from exc

    @app.get("/api/intelligence/calibration")
    async def calibration_summary(
        agent_id: str | None = Query(default=None),
        num_buckets: int = Query(default=10, ge=2, le=20),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        return prob_engine.compute_calibration_summary(agent_id=agent_id, num_buckets=num_buckets)

    @app.get("/api/intelligence/scorecards")
    async def agent_scorecards(
        min_resolved_samples: int = Query(default=10, ge=1, le=500),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        return prob_engine.get_agent_scorecards(min_resolved_samples=min_resolved_samples)

    @app.get("/api/intelligence/model-routing")
    async def model_routing_summary(
        limit: int = Query(default=50, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        return model_router.get_usage_summary(limit=limit)

    @app.post("/api/intelligence/model-routing/route")
    async def route_model_request(
        req: ModelRouteRequest,
        _: AuthenticatedUser = Depends(operator_model_guard),
    ) -> dict[str, Any]:
        try:
            return model_router.route_task(
                task_id=req.task_id,
                agent_id=req.agent_id,
                task_category=req.task_category,
                prefer_ai=req.prefer_ai,
                estimated_input_tokens=req.estimated_input_tokens,
                estimated_output_tokens=req.estimated_output_tokens,
                estimated_cost_override_usd=req.estimated_cost_override_usd,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=sanitize_error_message(exc)) from exc

    @app.get("/api/intelligence/configs")
    async def list_configs(
        config_domain: str | None = Query(default=None),
        limit: int = Query(default=60, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = config_manager.list_versions(config_domain=config_domain, limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/intelligence/configs/propose")
    async def propose_config(
        req: ConfigProposeRequest,
        user: AuthenticatedUser = Depends(analyst_guard),
    ) -> dict[str, Any]:
        try:
            return config_manager.propose_config_update(
                config_domain=req.config_domain,
                proposed_config=req.proposed_config,
                proposed_by=user.username,
                change_reason=req.change_reason,
                source_cycle_id=req.source_cycle_id,
                regression_case_id=req.regression_case_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/intelligence/configs/{config_version_id}/activate")
    async def activate_config(
        config_version_id: str,
        req: ConfigActivateRequest,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_config_guard),
    ) -> dict[str, Any]:
        try:
            activated = config_manager.activate_config_version(
                config_version_id,
                reviewed_by=owner.username,
            )
            auth_manager.record_audit_event(
                event_type="CONFIG_ACTIVATED",
                outcome="SUCCESS",
                ip_address=resolve_client_ip(request, sec_settings),
                endpoint=request.url.path,
                method="POST",
                user_id=owner.user_id,
                username=owner.username,
                role=owner.role,
                detail={"config_version_id": config_version_id},
            )
            return activated
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            msg = str(exc)
            code = 404 if "Unknown config_version_id" in msg else 400
            raise HTTPException(status_code=code, detail=msg) from exc

    @app.post("/api/intelligence/configs/rollback")
    async def rollback_config(
        req: ConfigRollbackRequest,
        request: Request,
        owner: AuthenticatedUser = Depends(owner_config_guard),
    ) -> dict[str, Any]:
        try:
            rolled = config_manager.rollback_config(
                config_domain=req.config_domain,
                target_version=req.target_version,
                actor=owner.username,
                reason=req.reason,
            )
            auth_manager.record_audit_event(
                event_type="CONFIG_ROLLED_BACK",
                outcome="SUCCESS",
                ip_address=resolve_client_ip(request, sec_settings),
                endpoint="/api/intelligence/configs/rollback",
                method="POST",
                user_id=owner.user_id,
                username=owner.username,
                role=owner.role,
                detail={"config_domain": req.config_domain, "target_version": req.target_version},
            )
            return rolled
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/intelligence/command-center")
    async def intelligence_command_center(
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        cal_cfg = config_manager.get_active_config("CALIBRATION_PARAMS") or {}
        min_samples = int(
            (cal_cfg.get("config") or {}).get(
                "min_resolved_samples_for_comparison",
                prob_engine.DEFAULT_MIN_SAMPLES_FOR_COMPARISON,
            )
        )
        num_buckets = int((cal_cfg.get("config") or {}).get("num_calibration_buckets", 10))
        return {
            "phase": "Phase 4A — Authenticated Operations & Intelligence Command Center",
            "live_trading_enabled": False,
            "supervisor": _redact_supervisor_snapshot(supervisor.get_status_snapshot()),
            "tasks": _redact_tasks_for_http(supervisor.list_tasks(limit=80)),
            "blockers": supervisor.blocker_engine.list_blockers(limit=50),
            "calibration": prob_engine.compute_calibration_summary(num_buckets=num_buckets),
            "scorecards": prob_engine.get_agent_scorecards(min_resolved_samples=min_samples),
            "predictions": prob_engine.list_predictions(limit=80),
            "model_routing": model_router.get_usage_summary(limit=40),
            "configs": config_manager.list_versions(limit=60),
            "memory_counts": store.get_counts(),
            "learning_cycles": learning_engine.list_cycles(limit=40),
            "regression_cases": learning_engine.list_regression_cases(limit=40),
            "operational_health": health_monitor.get_health_report(),
            "system_events": store.list_system_events(limit=50),
            "soak_runs": soak_manager.list_runs(limit=25),
            "concurrency_stress_runs": concurrency_lab.list_runs(limit=20),
            "historical_validation": prob_engine.compute_historical_validation(min_resolved_samples=min_samples),
            "research_validations": research_validator.list_validations(limit=30),
            "adversarial_reviews": adversarial_engine.list_reviews(limit=30),
            "paper_stress_runs": paper_stress_lab.list_runs(limit=40),
            "generated_at": utc_now_iso(),
        }

    # ------------------------------------------------------------------
    # Stage 3E: Continuous Operations, Soak Framework & Validation Labs
    # ------------------------------------------------------------------
    @app.get("/api/operations/health")
    async def operations_health(
        long_running_threshold_seconds: float = Query(default=60.0, ge=5.0, le=3600.0),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        return health_monitor.get_health_report(
            long_running_threshold_seconds=long_running_threshold_seconds
        )

    @app.get("/api/operations/events")
    async def operations_events(
        severity: str | None = Query(default=None),
        limit: int = Query(default=80, ge=1, le=300),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = store.list_system_events(severity=severity, limit=limit)
        return {"count": len(items), "items": items}

    @app.get("/api/operations/soak")
    async def list_soak_runs(
        limit: int = Query(default=30, ge=1, le=100),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = soak_manager.list_runs(limit=limit)
        return {"count": len(items), "active_run": soak_manager.get_active_run(), "items": items}

    @app.post("/api/operations/soak/start")
    async def start_soak_run(
        req: SoakStartRequest,
        _: AuthenticatedUser = Depends(operator_lab_guard),
    ) -> dict[str, Any]:
        try:
            return soak_manager.start_soak(
                mode=req.mode,
                duration_seconds=req.duration_seconds,
                tick_interval_seconds=req.tick_interval_seconds,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/operations/soak/stop")
    async def stop_soak_run(
        req: SoakStopRequest,
        _: AuthenticatedUser = Depends(operator_lab_guard),
    ) -> dict[str, Any]:
        try:
            return soak_manager.stop_soak(run_id=req.run_id, reason=req.reason)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/validation/concurrency-stress")
    async def list_concurrency_stress(
        limit: int = Query(default=25, ge=1, le=100),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = concurrency_lab.list_runs(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/validation/concurrency-stress")
    async def run_concurrency_stress(
        req: ConcurrencyStressRequest,
        _: AuthenticatedUser = Depends(operator_lab_guard),
    ) -> dict[str, Any]:
        return await concurrency_lab.run_stress_suite(
            task_count=req.task_count,
            worker_count=req.worker_count,
        )

    @app.get("/api/validation/predictions/historical")
    async def historical_prediction_validation(
        agent_id: str | None = Query(default=None),
        market_category: str | None = Query(default=None),
        min_resolved_samples: int = Query(default=10, ge=1, le=500),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        return prob_engine.compute_historical_validation(
            agent_id=agent_id,
            market_category=market_category,
            min_resolved_samples=min_resolved_samples,
        )

    @app.get("/api/validation/research")
    async def list_research_validations(
        limit: int = Query(default=50, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = research_validator.list_validations(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/validation/research")
    async def validate_research_submission(
        req: ResearchValidationRequest,
        _: AuthenticatedUser = Depends(analyst_guard),
    ) -> dict[str, Any]:
        return research_validator.validate_submission(
            agent_id=req.agent_id,
            source_identifier=req.source_identifier,
            source_timestamp=req.source_timestamp,
            retrieval_timestamp=req.retrieval_timestamp,
            evidence_summary=req.evidence_summary,
            market_id=req.market_id,
            market_question=req.market_question,
            raw_external_text=req.raw_external_text,
            confidence=req.confidence,
            conflicting_claims=req.conflicting_claims,
            cross_venue_check=req.cross_venue_check,
            max_age_ms=req.max_age_ms,
        )

    @app.get("/api/validation/paper-stress")
    async def list_paper_stress(
        limit: int = Query(default=60, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = paper_stress_lab.list_runs(limit=limit)
        return {"count": len(items), "provenance": "SIMULATED", "items": items}

    @app.post("/api/validation/paper-stress")
    async def run_paper_stress(
        req: PaperStressRequest,
        _: AuthenticatedUser = Depends(operator_lab_guard),
    ) -> dict[str, Any]:
        try:
            return await paper_stress_lab.run_scenario_suite(
                scenarios=req.scenarios,
                target_shares=req.target_shares,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/validation/adversarial-review")
    async def list_adversarial_reviews(
        limit: int = Query(default=50, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = adversarial_engine.list_reviews(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/validation/adversarial-review")
    async def run_adversarial_review(
        req: AdversarialReviewRequest,
        _: AuthenticatedUser = Depends(analyst_guard),
    ) -> dict[str, Any]:
        try:
            return adversarial_engine.run_review(
                subject_key=req.subject_key,
                conclusion_agent=req.conclusion_agent,
                conclusion_summary=req.conclusion_summary,
                proposer_evidence=req.proposer_evidence,
                challenger_agent=req.challenger_agent,
                arbiter_agent=req.arbiter_agent,
                metrics=req.metrics,
                opposing_evidence=req.opposing_evidence,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Kalshi Read-Only, Cross-Venue & Paper Execution Routes
    # ------------------------------------------------------------------
    @app.get("/api/kalshi/markets")
    async def kalshi_markets(
        limit: int = Query(default=15, ge=1, le=40),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
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
    async def kalshi_market_book(
        ticker: str,
        depth: int = Query(default=15, ge=1, le=50),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
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
    async def list_cross_venue_pairs(
        limit: int = Query(default=100, ge=1, le=200),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        pairs = store.list_cross_venue_pairs(limit=limit)
        items = [{**p, "review_state": pair_review_state(p)} for p in pairs]
        return {"count": len(items), "items": items}

    @app.get("/api/cross-venue/pairs/{pair_id}/review")
    async def review_cross_venue_pair(
        pair_id: str,
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        pair = store.get_cross_venue_pair(pair_id)
        if not pair:
            raise HTTPException(status_code=404, detail=f"Unknown cross-venue pair_id '{pair_id}'")
        try:
            return await build_pair_review(pair)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Pair review fetch failed: {sanitize_error_message(exc)}",
            ) from exc

    @app.post("/api/cross-venue/pairs/{pair_id}/reject")
    async def reject_cross_venue_pair(
        pair_id: str,
        req: CrossVenuePairRejectRequest,
        _: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        pair = store.get_cross_venue_pair(pair_id)
        if not pair:
            raise HTTPException(status_code=404, detail=f"Unknown cross-venue pair_id '{pair_id}'")
        record = build_rejected_record(pair, req.reason)
        store.upsert_cross_venue_pair(record)
        return {"pair": {**record, "review_state": pair_review_state(record)}}

    @app.post("/api/cross-venue/pairs")
    async def register_cross_venue_pair(
        req: CrossVenuePairRegisterRequest,
        _: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        try:
            poly_mkt = await app.state.poly_client.fetch_market(req.poly_market_id.strip())
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Polymarket metadata fetch failed: {sanitize_error_message(exc)}",
            ) from exc
        if poly_mkt is None:
            raise HTTPException(
                status_code=404,
                detail=f"Polymarket market '{req.poly_market_id}' not found or non-binary",
            )

        try:
            kalshi_mkt = await app.state.kalshi_client.fetch_market(req.kalshi_ticker.strip())
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Kalshi metadata fetch failed: {sanitize_error_message(exc)}",
            ) from exc
        if kalshi_mkt is None:
            raise HTTPException(
                status_code=404,
                detail=f"Kalshi market '{req.kalshi_ticker}' not found or non-binary",
            )

        assessment = assess_contract_equivalence(
            poly_market=poly_mkt,
            kalshi_market=kalshi_mkt,
            resolution_source_verified=req.resolution_source_verified,
            predicate_verified=req.predicate_verified,
            polarity_inverted=req.polarity_inverted,
            void_rules_compatible=req.void_rules_compatible,
            allow_early_close_variance=req.allow_early_close_variance,
        )
        pair_id = (
            req.pair_id.strip()
            if req.pair_id
            else f"XPAIR-{kalshi_mkt.ticker[:14]}-{poly_mkt.market_id[:8]}".upper()
        )
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

    @app.post("/api/cross-venue/discover")
    async def discover_cross_venue_pairs(
        req: CrossVenueDiscoverRequest,
        _: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        """Discover Polymarket<->Kalshi candidate pairs from live read-only data.

        Candidates are never settlement-verified; registered pairs stay denied by
        the Risk Gate until an operator attests them via POST /api/cross-venue/pairs.
        """
        try:
            return await discover_pairs(
                store,
                register=req.persist,
                poly_pages=req.poly_pages,
                kalshi_max_pages=req.kalshi_max_pages,
                min_similarity=req.min_similarity,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Cross-venue discovery failed: {sanitize_error_message(exc)}",
            ) from exc

    @app.get("/api/cross-venue/opportunities")
    async def list_cross_venue_opportunities(
        limit: int = Query(default=100, ge=1, le=300),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
        items = store.list_cross_venue_opportunities(limit=limit)
        return {"count": len(items), "items": items}

    @app.post("/api/cross-venue/scan")
    async def scan_cross_venue(
        req: CrossVenueScanRequest,
        _: AuthenticatedUser = Depends(operator_guard),
    ) -> dict[str, Any]:
        if req.pair_id:
            pair = store.get_cross_venue_pair(req.pair_id)
            if not pair:
                raise HTTPException(
                    status_code=404, detail=f"Unknown cross-venue pair_id '{req.pair_id}'"
                )
            pairs = [pair]
        else:
            # Confirmed pairs first, then pending; rejected pairs are skipped.
            pairs = prioritize_pairs_for_scan(store.list_cross_venue_pairs(limit=200), limit=25)

        if not pairs:
            return {"count": 0, "results": [], "message": "No cross-venue pairs registered yet."}

        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for pair in pairs:
            try:
                poly_mkt = await app.state.poly_client.fetch_market(pair["poly_market_id"])
                kalshi_mkt = await app.state.kalshi_client.fetch_market(pair["kalshi_ticker"])
                if poly_mkt is None or kalshi_mkt is None:
                    errors.append(
                        {"pair_id": pair["pair_id"], "error": "Market inactive or unavailable"}
                    )
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
                        **{
                            k: v
                            for k, v in res.items()
                            if k
                            not in (
                                "best_route",
                                "assessment",
                                "poly_yes_book",
                                "poly_no_book",
                                "kalshi_yes_book",
                                "kalshi_no_book",
                            )
                        },
                        "settlement_checks": list(res["assessment"].checks),
                        "mismatch_reasons": list(res["assessment"].mismatch_reasons),
                    }
                )
            except Exception as exc:
                errors.append({"pair_id": pair["pair_id"], "error": sanitize_error_message(exc)})

        return {"provenance": "LIVE", "count": len(results), "results": results, "errors": errors}

    @app.post("/api/paper/execute")
    async def execute_paper_trade(
        req: PaperExecutionRequest,
        request: Request,
        user: AuthenticatedUser = Depends(operator_paper_guard),
    ) -> dict[str, Any]:
        auth_manager.record_audit_event(
            event_type="PAPER_EXECUTION_TRIGGERED",
            outcome="ATTEMPT",
            ip_address=resolve_client_ip(request, sec_settings),
            endpoint="/api/paper/execute",
            method="POST",
            user_id=user.user_id,
            username=user.username,
            role=user.role,
            detail={
                "pair_id": req.pair_id,
                "target_shares": req.target_shares,
                "scenario": req.scenario,
            },
        )
        try:
            return await paper_engine.execute(req)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=sanitize_error_message(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Paper execution book fetch failed: {sanitize_error_message(exc)}",
            ) from exc

    @app.get("/api/paper/blotter")
    async def paper_blotter(
        limit: int = Query(default=100, ge=1, le=300),
        _: AuthenticatedUser = Depends(read_only_guard),
    ) -> dict[str, Any]:
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
    async def modules_status(_: AuthenticatedUser = Depends(read_only_guard)) -> dict[str, Any]:
        counts = store.get_counts()
        return {
            "phase": "Phase 4A — Authenticated Research & Paper Execution",
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
