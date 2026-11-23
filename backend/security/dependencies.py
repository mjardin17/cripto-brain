"""FastAPI dependencies & security helpers for deny-by-default Auth, RBAC, CSRF, Origin, TLS, and Rate Limiting."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable

from fastapi import HTTPException, Request, Response

from backend.security.auth import ROLE_LEVELS, AuthManager, AuthenticatedUser
from backend.security.config import (
    ARENA_SERVICE_TOKEN_HEADER,
    ARENA_SIGNATURE_HEADER,
    ARENA_TIMESTAMP_HEADER,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SecuritySettings,
    is_allowed_origin,
    resolve_client_ip,
    resolve_cookie_attributes,
    resolve_request_scheme,
)
from backend.security.rate_limit import DuckDBRateLimiter


def _append_partitioned_to_latest_cookie(response: Response) -> None:
    for idx in range(len(response.raw_headers) - 1, -1, -1):
        name, val = response.raw_headers[idx]
        if name.lower() == b"set-cookie":
            if b"partitioned" not in val.lower():
                response.raw_headers[idx] = (name, val + b"; Partitioned")
            break


def set_auth_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    settings: SecuritySettings,
    request: Request | None = None,
) -> None:
    secure, samesite, partitioned = resolve_cookie_attributes(request, settings)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=secure,
        samesite=samesite,  # type: ignore[arg-type]
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    if partitioned:
        _append_partitioned_to_latest_cookie(response)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,  # Readable by frontend SPA for X-CSRF-Token double-submit
        secure=secure,
        samesite=samesite,  # type: ignore[arg-type]
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    if partitioned:
        _append_partitioned_to_latest_cookie(response)


def clear_auth_cookies(
    response: Response,
    *,
    settings: SecuritySettings,
    request: Request | None = None,
) -> None:
    secure, samesite, partitioned = resolve_cookie_attributes(request, settings)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=secure,
        samesite=samesite,  # type: ignore[arg-type]
    )
    if partitioned:
        _append_partitioned_to_latest_cookie(response)
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        samesite=samesite,  # type: ignore[arg-type]
    )
    if partitioned:
        _append_partitioned_to_latest_cookie(response)


def enforce_tls_and_origin(
    request: Request,
    *,
    settings: SecuritySettings,
    auth_manager: AuthManager,
    require_origin_for_unsafe: bool = True,
) -> None:
    client_ip = resolve_client_ip(request, settings)
    scheme = resolve_request_scheme(request, settings)

    if settings.is_production and settings.require_tls_in_production and scheme != "https":
        auth_manager.record_audit_event(
            event_type="TLS_ENFORCEMENT_BLOCK",
            outcome="DENIED",
            ip_address=client_ip,
            endpoint=request.url.path,
            method=request.method,
            detail={"observed_scheme": scheme},
        )
        raise HTTPException(
            status_code=403,
            detail="HTTPS transport is required in production.",
        )

    if require_origin_for_unsafe and request.method.upper() not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        candidate = origin or referer
        if candidate is not None and not is_allowed_origin(candidate, settings, request):
            auth_manager.record_audit_event(
                event_type="ORIGIN_VALIDATION_BLOCK",
                outcome="DENIED",
                ip_address=client_ip,
                endpoint=request.url.path,
                method=request.method,
                detail={"origin": candidate[:120]},
            )
            raise HTTPException(status_code=403, detail="Origin not allowed.")
        if settings.is_production and candidate is None:
            auth_manager.record_audit_event(
                event_type="ORIGIN_MISSING_BLOCK",
                outcome="DENIED",
                ip_address=client_ip,
                endpoint=request.url.path,
                method=request.method,
                detail={"reason": "Missing Origin/Referer on unsafe production request"},
            )
            raise HTTPException(status_code=403, detail="Origin header required in production.")


def enforce_rate_limit_check(
    request: Request,
    *,
    rate_limiter: DuckDBRateLimiter,
    auth_manager: AuthManager,
    settings: SecuritySettings,
    bucket_class: str,
    identifier: str,
    user: AuthenticatedUser | None = None,
) -> None:
    decision = rate_limiter.check_and_consume(bucket_class, identifier)
    if not decision.allowed:
        client_ip = resolve_client_ip(request, settings)
        auth_manager.record_audit_event(
            event_type="RATE_LIMIT_BLOCKED",
            outcome="DENIED",
            ip_address=client_ip,
            endpoint=request.url.path,
            method=request.method,
            user_id=user.user_id if user else None,
            username=user.username if user else None,
            role=user.role if user else None,
            detail={
                "bucket_class": bucket_class,
                "limit": decision.limit,
                "retry_after_seconds": decision.retry_after_seconds,
            },
        )
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded for {bucket_class}. Retry after {decision.retry_after_seconds}s.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def extract_session_token(request: Request) -> tuple[str | None, bool]:
    """Return (raw_session_token, from_cookie) preferring HttpOnly cookie over Bearer fallback."""
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token, True
    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:].strip()
        if len(bearer_token) >= 16:
            return bearer_token, False
    return None, False


def optional_authenticated_user(request: Request) -> AuthenticatedUser | None:
    auth_manager: AuthManager = request.app.state.auth_manager
    raw_token, _ = extract_session_token(request)
    return auth_manager.validate_session(raw_token)


def make_guard(
    min_role: str = "READ_ONLY",
    *,
    rate_limit_class: str | None = None,
    require_csrf: bool | None = None,
) -> Callable[[Request], AuthenticatedUser]:
    """Create a FastAPI dependency enforcing Auth, RBAC, CSRF, Origin, TLS, and Rate Limit."""
    if min_role not in ROLE_LEVELS:
        raise ValueError(f"Invalid min_role '{min_role}'")

    def _dependency(request: Request) -> AuthenticatedUser:
        settings: SecuritySettings = request.app.state.security_settings
        auth_manager: AuthManager = request.app.state.auth_manager
        rate_limiter: DuckDBRateLimiter = request.app.state.rate_limiter
        client_ip = resolve_client_ip(request, settings)

        enforce_tls_and_origin(
            request,
            settings=settings,
            auth_manager=auth_manager,
            require_origin_for_unsafe=True,
        )

        raw_token, from_cookie = extract_session_token(request)
        user = auth_manager.validate_session(raw_token)
        if user is None:
            auth_manager.record_audit_event(
                event_type="AUTH_UNAUTHENTICATED_BLOCK",
                outcome="DENIED",
                ip_address=client_ip,
                endpoint=request.url.path,
                method=request.method,
                detail={"has_cookie": bool(raw_token and from_cookie)},
            )
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Please sign in.",
            )

        is_unsafe = request.method.upper() not in ("GET", "HEAD", "OPTIONS")
        do_csrf = is_unsafe if require_csrf is None else require_csrf
        if do_csrf:
            csrf_header = request.headers.get(CSRF_HEADER_NAME)
            csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
            csrf_ok = (
                bool(csrf_header)
                and auth_manager.verify_csrf(user, csrf_header)
                and (
                    not from_cookie
                    or (bool(csrf_cookie) and csrf_header.strip() == csrf_cookie.strip())
                )
            )
            if not csrf_ok:
                auth_manager.record_audit_event(
                    event_type="CSRF_VALIDATION_BLOCK",
                    outcome="DENIED",
                    ip_address=client_ip,
                    endpoint=request.url.path,
                    method=request.method,
                    user_id=user.user_id,
                    username=user.username,
                    role=user.role,
                    detail={"has_header": bool(csrf_header), "has_cookie": bool(csrf_cookie)},
                )
                raise HTTPException(
                    status_code=403,
                    detail="CSRF validation failed.",
                )

        if not user.has_role_at_least(min_role):
            auth_manager.record_audit_event(
                event_type="RBAC_FORBIDDEN_BLOCK",
                outcome="DENIED",
                ip_address=client_ip,
                endpoint=request.url.path,
                method=request.method,
                user_id=user.user_id,
                username=user.username,
                role=user.role,
                detail={"required_role": min_role, "actual_role": user.role},
            )
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions: '{min_role}' role required (current: '{user.role}').",
            )

        rl_class = rate_limit_class or ("MUTATION_GENERAL" if is_unsafe else "API_READ")
        enforce_rate_limit_check(
            request,
            rate_limiter=rate_limiter,
            auth_manager=auth_manager,
            settings=settings,
            bucket_class=rl_class,
            identifier=f"user:{user.user_id}:{client_ip}",
            user=user,
        )
        return user

    return _dependency


def compute_arena_request_signature(
    *,
    hmac_secret: str,
    timestamp_str: str,
    body_bytes: bytes,
) -> str:
    """Compute canonical HMAC-SHA256 hex digest over `{timestamp}.{body}` for Arena service sync."""
    msg = timestamp_str.encode("utf-8") + b"." + body_bytes
    return hmac.new(hmac_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _extract_arena_service_token(request: Request) -> str | None:
    direct = request.headers.get(ARENA_SERVICE_TOKEN_HEADER, "").strip()
    if direct:
        return direct
    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.lower().startswith("arenaservice "):
        token = auth_header[len("arenaservice ") :].strip()
        if token:
            return token
    return None


def make_arena_ingest_guard(
    *,
    min_role: str = "ANALYST",
    rate_limit_class: str = "MUTATION_GENERAL",
    max_signature_skew_seconds: int = 300,
) -> Callable[[Request], AuthenticatedUser]:
    """Allow EITHER an authenticated Analyst+ session OR an authenticated AI Orchestra Arena service caller.

    Service callers authenticate with `X-Arena-Service-Token` (or `Authorization: ArenaService <token>`)
    validated in constant time against `CERTAINTY_ARENA_SERVICE_TOKEN` (>= 24 chars).
    If `CERTAINTY_ARENA_HMAC_SECRET` is configured (or if an HMAC signature header is sent),
    `X-Arena-Timestamp` + `X-Arena-Signature` must also match HMAC-SHA256 over `{ts}.{raw_body}`
    within the replay window.
    """
    session_guard = make_guard(min_role, rate_limit_class=rate_limit_class)

    async def _dependency(request: Request) -> AuthenticatedUser:
        provided_token = _extract_arena_service_token(request)
        if provided_token is None:
            return session_guard(request)

        settings: SecuritySettings = request.app.state.security_settings
        auth_manager: AuthManager = request.app.state.auth_manager
        rate_limiter: DuckDBRateLimiter = request.app.state.rate_limiter
        client_ip = resolve_client_ip(request, settings)

        # Service-to-service calls still enforce TLS in production; Origin is not required for non-browser callers.
        enforce_tls_and_origin(
            request,
            settings=settings,
            auth_manager=auth_manager,
            require_origin_for_unsafe=False,
        )

        expected_token = settings.arena_service_token.strip()
        if len(expected_token) < 24 or not hmac.compare_digest(provided_token, expected_token):
            auth_manager.record_audit_event(
                event_type="ARENA_SERVICE_AUTH_DENIED",
                outcome="DENIED",
                ip_address=client_ip,
                endpoint=request.url.path,
                method=request.method,
                detail={"reason": "invalid_or_unconfigured_service_token"},
            )
            raise HTTPException(
                status_code=401,
                detail="Invalid or unconfigured Arena service authentication token.",
            )

        expected_hmac = settings.arena_hmac_secret.strip()
        provided_sig = request.headers.get(ARENA_SIGNATURE_HEADER, "").strip().lower()
        provided_ts = request.headers.get(ARENA_TIMESTAMP_HEADER, "").strip()
        if expected_hmac or provided_sig:
            if not expected_hmac or not provided_sig or not provided_ts:
                auth_manager.record_audit_event(
                    event_type="ARENA_SERVICE_AUTH_DENIED",
                    outcome="DENIED",
                    ip_address=client_ip,
                    endpoint=request.url.path,
                    method=request.method,
                    detail={"reason": "missing_hmac_signature_or_timestamp"},
                )
                raise HTTPException(
                    status_code=401,
                    detail="Arena service request requires valid X-Arena-Timestamp and X-Arena-Signature.",
                )
            try:
                ts_num = int(provided_ts)
            except ValueError:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid X-Arena-Timestamp header.",
                ) from None
            if abs(int(time.time()) - ts_num) > max_signature_skew_seconds:
                auth_manager.record_audit_event(
                    event_type="ARENA_SERVICE_AUTH_DENIED",
                    outcome="DENIED",
                    ip_address=client_ip,
                    endpoint=request.url.path,
                    method=request.method,
                    detail={"reason": "hmac_timestamp_outside_replay_window"},
                )
                raise HTTPException(
                    status_code=401,
                    detail="Arena service signature timestamp outside allowed replay window.",
                )
            body_bytes = await request.body()
            expected_sig = compute_arena_request_signature(
                hmac_secret=expected_hmac,
                timestamp_str=provided_ts,
                body_bytes=body_bytes,
            )
            if not hmac.compare_digest(provided_sig, expected_sig):
                auth_manager.record_audit_event(
                    event_type="ARENA_SERVICE_AUTH_DENIED",
                    outcome="DENIED",
                    ip_address=client_ip,
                    endpoint=request.url.path,
                    method=request.method,
                    detail={"reason": "hmac_signature_mismatch"},
                )
                raise HTTPException(
                    status_code=401,
                    detail="Arena service HMAC signature verification failed.",
                )

        svc_user = AuthenticatedUser(
            user_id="svc-ai-orchestra-arena",
            username="ai_orchestra_arena",
            email="arena-service@certainty.local",
            role="ANALYST",
            session_id="arena-service-token",
            csrf_token_hash="",
            is_active=True,
            must_rotate_password=False,
        )
        enforce_rate_limit_check(
            request,
            rate_limiter=rate_limiter,
            auth_manager=auth_manager,
            settings=settings,
            bucket_class=rate_limit_class,
            identifier=f"service:arena:{client_ip}",
            user=svc_user,
        )
        return svc_user

    return _dependency

