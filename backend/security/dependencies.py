"""FastAPI dependencies & security helpers for deny-by-default Auth, RBAC, CSRF, Origin, TLS, and Rate Limiting."""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request, Response

from backend.security.auth import ROLE_LEVELS, AuthManager, AuthenticatedUser
from backend.security.config import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SecuritySettings,
    is_allowed_origin,
    resolve_client_ip,
    resolve_request_scheme,
)
from backend.security.rate_limit import DuckDBRateLimiter


def set_auth_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    settings: SecuritySettings,
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,  # Readable by frontend SPA for X-CSRF-Token double-submit
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        max_age=settings.session_ttl_seconds,
        path="/",
    )


def clear_auth_cookies(response: Response, *, settings: SecuritySettings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
    )
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
    )


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


def optional_authenticated_user(request: Request) -> AuthenticatedUser | None:
    auth_manager: AuthManager = request.app.state.auth_manager
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
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

        raw_token = request.cookies.get(SESSION_COOKIE_NAME)
        user = auth_manager.validate_session(raw_token)
        if user is None:
            auth_manager.record_audit_event(
                event_type="AUTH_UNAUTHENTICATED_BLOCK",
                outcome="DENIED",
                ip_address=client_ip,
                endpoint=request.url.path,
                method=request.method,
                detail={"has_cookie": bool(raw_token)},
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
            if (
                not csrf_header
                or not csrf_cookie
                or csrf_header.strip() != csrf_cookie.strip()
                or not auth_manager.verify_csrf(user, csrf_header)
            ):
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
