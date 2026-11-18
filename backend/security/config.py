"""Security configuration, TLS enforcement, Origin/CORS checks, and trusted-proxy resolution."""

from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlparse

from fastapi import Request

SESSION_COOKIE_NAME = "certainty_session"
CSRF_COOKIE_NAME = "certainty_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"

_INSECURE_PLACEHOLDERS = (
    "changeme",
    "placeholder",
    "default",
    "my_secret",
    "secret_here",
    "example",
    "test-only-insecure",
)


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _parse_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class SecuritySettings:
    env_mode: str = "development"
    session_secret: str = ""
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    session_ttl_seconds: int = 43200  # 12 hours
    require_tls_in_production: bool = True
    allowed_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8010",
        "http://127.0.0.1:8010",
    )
    trusted_proxies: tuple[str, ...] = ("127.0.0.1", "::1")
    rate_limit_enabled: bool = True
    min_password_length: int = 12
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    bootstrap_token: str = ""
    rate_limit_overrides: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def is_production(self) -> bool:
        return self.env_mode.strip().lower() == "production"


def load_security_settings(
    *,
    env_override: dict[str, str] | None = None,
) -> SecuritySettings:
    env = os.environ if env_override is None else env_override
    env_mode = env.get("CERTAINTY_ENV", "development").strip().lower() or "development"
    is_prod = env_mode == "production"

    raw_secret = env.get("CERTAINTY_SESSION_SECRET", "").strip()
    if not raw_secret and not is_prod:
        # Ephemeral per-process CSPRNG fallback for local dev/tests only.
        raw_secret = secrets.token_urlsafe(48)

    cookie_secure = _parse_bool(env.get("CERTAINTY_COOKIE_SECURE"), default=is_prod)

    default_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8010",
        "http://127.0.0.1:8010",
    ]
    app_url = env.get("APP_URL", "").strip()
    if app_url and app_url != "MY_APP_URL":
        parsed = urlparse(app_url)
        if parsed.scheme and parsed.netloc:
            default_origins.append(f"{parsed.scheme}://{parsed.netloc}")

    raw_origins = env.get("CERTAINTY_ALLOWED_ORIGINS", "").strip()
    allowed_origins = _parse_csv(raw_origins) if raw_origins else tuple(dict.fromkeys(default_origins))

    raw_proxies = env.get("CERTAINTY_TRUSTED_PROXIES", "").strip()
    trusted_proxies = _parse_csv(raw_proxies) if raw_proxies else ("127.0.0.1", "::1")

    rate_limit_enabled = _parse_bool(env.get("CERTAINTY_RATE_LIMIT_ENABLED"), default=True)
    bootstrap_token = env.get("CERTAINTY_BOOTSTRAP_TOKEN", "").strip()
    require_tls = _parse_bool(env.get("CERTAINTY_REQUIRE_TLS"), default=True)

    return SecuritySettings(
        env_mode=env_mode,
        session_secret=raw_secret,
        cookie_secure=cookie_secure,
        session_ttl_seconds=max(300, int(env.get("CERTAINTY_SESSION_TTL_SECONDS", "43200"))),
        require_tls_in_production=require_tls,
        allowed_origins=allowed_origins,
        trusted_proxies=trusted_proxies,
        rate_limit_enabled=rate_limit_enabled,
        min_password_length=max(12, int(env.get("CERTAINTY_MIN_PASSWORD_LENGTH", "12"))),
        max_failed_logins=max(3, int(env.get("CERTAINTY_MAX_FAILED_LOGINS", "5"))),
        lockout_minutes=max(1, int(env.get("CERTAINTY_LOCKOUT_MINUTES", "15"))),
        bootstrap_token=bootstrap_token,
    )


def validate_security_settings(settings: SecuritySettings) -> None:
    """Fail closed at startup if production security invariants are violated."""
    if settings.env_mode not in ("development", "production", "test"):
        raise RuntimeError(f"Unsupported CERTAINTY_ENV '{settings.env_mode}'")

    if len(settings.session_secret.strip()) < 32:
        raise RuntimeError(
            "CERTAINTY_SESSION_SECRET must be at least 32 characters long."
        )
    low_sec = settings.session_secret.lower()
    if any(p in low_sec for p in _INSECURE_PLACEHOLDERS):
        raise RuntimeError("CERTAINTY_SESSION_SECRET contains an insecure placeholder pattern.")

    if settings.is_production:
        if not settings.cookie_secure:
            raise RuntimeError(
                "Production startup refused: CERTAINTY_COOKIE_SECURE must be true in production."
            )
        if not settings.require_tls_in_production:
            raise RuntimeError(
                "Production startup refused: TLS enforcement cannot be disabled in production."
            )
        if not settings.allowed_origins or any(o == "*" for o in settings.allowed_origins):
            raise RuntimeError(
                "Production startup refused: wildcard or empty CERTAINTY_ALLOWED_ORIGINS is forbidden."
            )
        for origin in settings.allowed_origins:
            if not origin.startswith("https://"):
                raise RuntimeError(
                    f"Production startup refused: allowed origin '{origin}' must use https://"
                )


def _ip_in_trusted_proxies(ip_str: str, trusted_proxies: Iterable[str]) -> bool:
    if not ip_str:
        return False
    cleaned = ip_str.strip()
    if cleaned in trusted_proxies:
        return True
    try:
        addr = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    for entry in trusted_proxies:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ipaddress.ip_address(entry) == addr:
                return True
        except ValueError:
            continue
    return False


def is_trusted_proxy_peer(request: Request, settings: SecuritySettings) -> bool:
    peer_host = request.client.host if request.client else ""
    return _ip_in_trusted_proxies(peer_host, settings.trusted_proxies)


def resolve_client_ip(request: Request, settings: SecuritySettings) -> str:
    """Return peer IP; inspect X-Forwarded-For ONLY when peer is in trusted_proxies."""
    peer_host = (request.client.host if request.client else "") or "unknown"
    if not is_trusted_proxy_peer(request, settings):
        return peer_host
    xff = request.headers.get("x-forwarded-for", "").strip()
    if not xff:
        return peer_host
    # Take the rightmost non-trusted hop if chained, or first provided hop from local trusted proxy
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _ip_in_trusted_proxies(hop, settings.trusted_proxies):
            return hop
    return hops[0] if hops else peer_host


def resolve_request_scheme(request: Request, settings: SecuritySettings) -> str:
    """Return effective transport scheme; honor X-Forwarded-Proto ONLY from trusted proxies."""
    scheme = (request.url.scheme or "http").lower()
    if not is_trusted_proxy_peer(request, settings):
        return scheme
    xfp = request.headers.get("x-forwarded-proto", "").strip().lower()
    if xfp:
        first_proto = xfp.split(",")[0].strip()
        if first_proto in ("http", "https"):
            return first_proto
    return scheme


def resolve_request_host(request: Request, settings: SecuritySettings) -> str:
    """Return effective host; honor X-Forwarded-Host ONLY from trusted proxies."""
    host_header = request.headers.get("host", "").strip().lower()
    if is_trusted_proxy_peer(request, settings):
        xfh = request.headers.get("x-forwarded-host", "").strip().lower()
        if xfh:
            first_host = xfh.split(",")[0].strip()
            if first_host:
                return first_host
    return host_header


def resolve_cookie_attributes(
    request: Request | None, settings: SecuritySettings
) -> tuple[bool, str, bool]:
    """Resolve (secure, samesite, partitioned) for auth cookies.

    Over HTTPS (including AI Studio Preview cross-site iframes), emit
    Secure + SameSite=None + Partitioned (CHIPS) unless explicitly pinned
    to SameSite=strict, so browsers transmit HttpOnly session cookies inside
    embedded previews as well as top-level tabs.
    Over plain HTTP (localhost / TestClient), keep secure=False and SameSite=lax.
    """
    eff_scheme = (
        resolve_request_scheme(request, settings)
        if request is not None
        else ("https" if settings.cookie_secure else "http")
    )
    if eff_scheme == "https":
        samesite = "strict" if settings.cookie_samesite == "strict" else "none"
        return True, samesite, True
    if settings.cookie_secure:
        return True, settings.cookie_samesite, False
    return False, settings.cookie_samesite, False


def normalize_origin(url_or_origin: str | None) -> str:
    if not url_or_origin:
        return ""
    parsed = urlparse(url_or_origin.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def is_allowed_origin(origin: str, settings: SecuritySettings, request: Request) -> bool:
    """Validate Origin/Referer against allowlist (or exact same host/scheme when Origin matches Host)."""
    norm = normalize_origin(origin)
    if not norm:
        return False
    allowed_set = {normalize_origin(o) for o in settings.allowed_origins if o != "*"}
    if norm in allowed_set:
        return True
    # Also permit same-origin request matching effective scheme + Host / X-Forwarded-Host header
    host_header = resolve_request_host(request, settings)
    if host_header:
        eff_scheme = resolve_request_scheme(request, settings)
        if norm == f"{eff_scheme}://{host_header}":
            return True
    return False
