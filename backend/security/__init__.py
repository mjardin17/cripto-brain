"""Stage 4A Authentication, RBAC, CSRF, Rate Limiting, and Security Audit Package."""

from backend.security.auth import (
    ROLE_LEVELS,
    VALID_ROLES,
    AuthManager,
    AuthenticatedUser,
    permissions_for_role,
    validate_password_strength,
)
from backend.security.config import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SecuritySettings,
    load_security_settings,
    validate_security_settings,
)
from backend.security.rate_limit import DEFAULT_RATE_LIMITS, DuckDBRateLimiter

__all__ = [
    "AuthManager",
    "AuthenticatedUser",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "DEFAULT_RATE_LIMITS",
    "DuckDBRateLimiter",
    "ROLE_LEVELS",
    "SESSION_COOKIE_NAME",
    "SecuritySettings",
    "VALID_ROLES",
    "load_security_settings",
    "permissions_for_role",
    "validate_password_strength",
    "validate_security_settings",
]
