"""Server-side Argon2id authentication, session lifecycle, RBAC, password resets, and immutable security audit logging."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from backend.security.config import SecuritySettings
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

VALID_ROLES: tuple[str, ...] = ("READ_ONLY", "ANALYST", "OPERATOR", "OWNER")
ROLE_LEVELS: dict[str, int] = {
    "READ_ONLY": 10,
    "ANALYST": 20,
    "OPERATOR": 30,
    "OWNER": 40,
}

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")
_PASSWORD_HASHER = PasswordHasher()
# Pre-computed dummy hash for constant-time verification when username does not exist
_DUMMY_ARGON2_HASH = _PASSWORD_HASHER.hash("Certainty-Dummy-Timing-Guard-99!")


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def permissions_for_role(role: str) -> dict[str, bool]:
    level = ROLE_LEVELS.get(role, 0)
    return {
        "can_read_dashboards": level >= ROLE_LEVELS["READ_ONLY"],
        "can_submit_research": level >= ROLE_LEVELS["ANALYST"],
        "can_evaluate_risk_dry_run": level >= ROLE_LEVELS["ANALYST"],
        "can_propose_configs": level >= ROLE_LEVELS["ANALYST"],
        "can_run_scans": level >= ROLE_LEVELS["OPERATOR"],
        "can_manage_pairs": level >= ROLE_LEVELS["OPERATOR"],
        "can_execute_paper_trades": level >= ROLE_LEVELS["OPERATOR"],
        "can_dispatch_agents": level >= ROLE_LEVELS["OPERATOR"],
        "can_scale_agents": level >= ROLE_LEVELS["OPERATOR"],
        "can_resolve_predictions": level >= ROLE_LEVELS["OPERATOR"],
        "can_route_models": level >= ROLE_LEVELS["OPERATOR"],
        "can_run_validation_labs": level >= ROLE_LEVELS["OPERATOR"],
        "can_toggle_kill_switch": level >= ROLE_LEVELS["OWNER"],
        "can_activate_configs": level >= ROLE_LEVELS["OWNER"],
        "can_manage_users": level >= ROLE_LEVELS["OWNER"],
        "can_manage_all_sessions": level >= ROLE_LEVELS["OWNER"],
        "can_view_security_audit": level >= ROLE_LEVELS["OWNER"],
    }


def validate_password_strength(
    password: str, *, username: str = "", min_length: int = 12
) -> None:
    if not isinstance(password, str) or len(password) < min_length:
        raise ValueError(f"Password must be at least {min_length} characters long.")
    if len(password) > 128:
        raise ValueError("Password must not exceed 128 characters.")
    if password.strip() != password:
        raise ValueError("Password must not begin or end with whitespace.")
    if username and username.lower() in password.lower():
        raise ValueError("Password must not contain the username.")

    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        raise ValueError(
            "Password must include at least 3 character classes (uppercase, lowercase, digits, symbols)."
        )


def validate_username(username: str) -> str:
    cleaned = username.strip()
    if not _USERNAME_RE.match(cleaned):
        raise ValueError(
            "Username must be 3-32 characters (letters, digits, '.', '_', '-'), starting with alphanumeric."
        )
    return cleaned


def validate_role(role: str) -> str:
    upper = role.strip().upper()
    if upper not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of {VALID_ROLES}.")
    return upper


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str
    email: str | None
    role: str
    session_id: str
    csrf_token_hash: str
    is_active: bool
    must_rotate_password: bool

    def has_role_at_least(self, minimum_role: str) -> bool:
        return ROLE_LEVELS.get(self.role, 0) >= ROLE_LEVELS.get(minimum_role, 999)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "session_id": self.session_id,
            "is_active": self.is_active,
            "must_rotate_password": self.must_rotate_password,
            "permissions": permissions_for_role(self.role),
        }


class AuthManager:
    """Server-authoritative user, session, password reset, and audit log manager."""

    def __init__(self, store: DuckDBStore, settings: SecuritySettings) -> None:
        self.store = store
        self.settings = settings

    # ------------------------------------------------------------------
    # Security Audit Log (Append-Only; No Update / No Delete)
    # ------------------------------------------------------------------
    def record_audit_event(
        self,
        *,
        event_type: str,
        outcome: str,
        ip_address: str = "internal",
        endpoint: str = "",
        method: str = "",
        user_id: str | None = None,
        username: str | None = None,
        role: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        audit_id = f"SAUD-{uuid.uuid4().hex[:12].upper()}"
        now_iso = utc_now_iso()
        # Strip any accidental sensitive keys if passed in detail
        safe_detail: dict[str, Any] = {}
        for k, v in (detail or {}).items():
            lk = str(k).lower()
            if any(s in lk for s in ("password", "secret", "token", "hash", "cookie")):
                safe_detail[k] = "[REDACTED]"
            else:
                safe_detail[k] = v

        with self.store._lock:
            self.store._conn.execute(
                "INSERT INTO security_audit_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    audit_id,
                    event_type.strip().upper(),
                    outcome.strip().upper(),
                    user_id,
                    username,
                    role,
                    ip_address[:128],
                    endpoint[:200],
                    method[:16],
                    json.dumps(safe_detail),
                    now_iso,
                ],
            )
        return {
            "audit_id": audit_id,
            "event_type": event_type.strip().upper(),
            "outcome": outcome.strip().upper(),
            "user_id": user_id,
            "username": username,
            "role": role,
            "ip_address": ip_address,
            "endpoint": endpoint,
            "method": method,
            "detail": safe_detail,
            "created_at": now_iso,
        }

    def list_audit_logs(
        self, *, event_type: str | None = None, outcome: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type.strip().upper())
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome.strip().upper())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM security_audit_log {where_sql} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["detail"] = json.loads(d.pop("detail_json", "{}"))
                out.append(d)
            return out

    # ------------------------------------------------------------------
    # User Management & Bootstrap
    # ------------------------------------------------------------------
    def _row_to_public_user(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d.pop("password_hash", None)
        return d

    def count_users(self) -> int:
        with self.store._lock:
            return int(self.store._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def needs_owner_bootstrap(self) -> bool:
        return self.count_users() == 0

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", [user_id]
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_public_user(cols, row)

    def list_users(self) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM users ORDER BY created_at ASC"
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_public_user(cols, r) for r in rows]

    def _active_owner_count(self) -> int:
        return int(
            self.store._conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'OWNER' AND is_active = TRUE"
            ).fetchone()[0]
        )

    def bootstrap_owner(
        self,
        *,
        username: str,
        password: str,
        email: str | None = None,
        bootstrap_token: str | None = None,
        ip_address: str = "internal",
    ) -> dict[str, Any]:
        """Create the initial OWNER only when zero users exist. Fails closed otherwise."""
        if self.settings.is_production:
            expected_tok = self.settings.bootstrap_token
            if not expected_tok or not bootstrap_token or not hmac.compare_digest(
                bootstrap_token.strip(), expected_tok
            ):
                self.record_audit_event(
                    event_type="OWNER_BOOTSTRAP",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/bootstrap-owner",
                    method="POST",
                    detail={"reason": "Invalid or missing production bootstrap token"},
                )
                raise PermissionError("Valid CERTAINTY_BOOTSTRAP_TOKEN is required in production.")
        elif self.settings.bootstrap_token:
            if not bootstrap_token or not hmac.compare_digest(
                bootstrap_token.strip(), self.settings.bootstrap_token
            ):
                self.record_audit_event(
                    event_type="OWNER_BOOTSTRAP",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/bootstrap-owner",
                    method="POST",
                    detail={"reason": "Invalid bootstrap token"},
                )
                raise PermissionError("Invalid bootstrap token.")

        uname = validate_username(username)
        validate_password_strength(
            password, username=uname, min_length=self.settings.min_password_length
        )
        pw_hash = _PASSWORD_HASHER.hash(password)
        now_iso = utc_now_iso()
        user_id = f"USR-{uuid.uuid4().hex[:10].upper()}"

        with self.store._lock:
            existing = int(self.store._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            if existing > 0:
                self.record_audit_event(
                    event_type="OWNER_BOOTSTRAP",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/bootstrap-owner",
                    method="POST",
                    detail={"reason": "Bootstrap refused because users already exist"},
                )
                raise RuntimeError("Initial OWNER already enrolled; bootstrap is permanently closed.")

            self.store._conn.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    user_id,
                    uname,
                    email.strip() if email else None,
                    "OWNER",
                    pw_hash,
                    True,
                    False,
                    0,
                    None,
                    None,
                    now_iso,
                    now_iso,
                    "initial_bootstrap",
                ],
            )
            self.record_audit_event(
                event_type="OWNER_BOOTSTRAP",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint="/api/auth/bootstrap-owner",
                method="POST",
                user_id=user_id,
                username=uname,
                role="OWNER",
                detail={"method": "explicit_enrollment"},
            )
            created = self.get_user_by_id(user_id)
            assert created is not None
            return created

    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str,
        email: str | None = None,
        must_rotate_password: bool = False,
        actor_username: str,
        actor_user_id: str | None = None,
        ip_address: str = "internal",
    ) -> dict[str, Any]:
        uname = validate_username(username)
        clean_role = validate_role(role)
        validate_password_strength(
            password, username=uname, min_length=self.settings.min_password_length
        )
        pw_hash = _PASSWORD_HASHER.hash(password)
        now_iso = utc_now_iso()
        user_id = f"USR-{uuid.uuid4().hex[:10].upper()}"

        with self.store._lock:
            dup = self.store._conn.execute(
                "SELECT 1 FROM users WHERE lower(username) = lower(?)", [uname]
            ).fetchone()
            if dup:
                raise ValueError(f"Username '{uname}' already exists.")
            self.store._conn.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    user_id,
                    uname,
                    email.strip() if email else None,
                    clean_role,
                    pw_hash,
                    True,
                    bool(must_rotate_password),
                    0,
                    None,
                    None,
                    now_iso,
                    now_iso,
                    actor_username,
                ],
            )
            self.record_audit_event(
                event_type="USER_CREATED",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint="/api/admin/users",
                method="POST",
                user_id=actor_user_id,
                username=actor_username,
                role="OWNER",
                detail={"target_user_id": user_id, "target_username": uname, "assigned_role": clean_role},
            )
            created = self.get_user_by_id(user_id)
            assert created is not None
            return created

    def update_user_role(
        self,
        target_user_id: str,
        *,
        new_role: str,
        actor_username: str,
        actor_user_id: str,
        ip_address: str = "internal",
    ) -> dict[str, Any]:
        clean_role = validate_role(new_role)
        now_iso = utc_now_iso()
        with self.store._lock:
            target = self.get_user_by_id(target_user_id)
            if not target:
                raise ValueError(f"Unknown user_id '{target_user_id}'")
            old_role = target["role"]
            if old_role == "OWNER" and clean_role != "OWNER" and target["is_active"]:
                if self._active_owner_count() <= 1:
                    raise ValueError("Cannot demote the last active OWNER.")

            self.store._conn.execute(
                "UPDATE users SET role = ?, updated_at = ? WHERE user_id = ?",
                [clean_role, now_iso, target_user_id],
            )
            # Immediately revoke active sessions so role snapshot and permissions reset cleanly
            self.revoke_all_user_sessions(
                target_user_id,
                reason=f"Role changed from {old_role} to {clean_role} by {actor_username}",
            )
            self.record_audit_event(
                event_type="USER_ROLE_CHANGED",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint=f"/api/admin/users/{target_user_id}/role",
                method="PATCH",
                user_id=actor_user_id,
                username=actor_username,
                role="OWNER",
                detail={
                    "target_user_id": target_user_id,
                    "target_username": target["username"],
                    "old_role": old_role,
                    "new_role": clean_role,
                },
            )
            updated = self.get_user_by_id(target_user_id)
            assert updated is not None
            return updated

    def set_user_active(
        self,
        target_user_id: str,
        *,
        is_active: bool,
        actor_username: str,
        actor_user_id: str,
        ip_address: str = "internal",
    ) -> dict[str, Any]:
        now_iso = utc_now_iso()
        with self.store._lock:
            target = self.get_user_by_id(target_user_id)
            if not target:
                raise ValueError(f"Unknown user_id '{target_user_id}'")
            if not is_active and target["role"] == "OWNER" and target["is_active"]:
                if self._active_owner_count() <= 1:
                    raise ValueError("Cannot deactivate the last active OWNER.")

            self.store._conn.execute(
                """
                UPDATE users SET
                    is_active = ?,
                    failed_login_attempts = CASE WHEN ? THEN 0 ELSE failed_login_attempts END,
                    locked_until = CASE WHEN ? THEN NULL ELSE locked_until END,
                    updated_at = ?
                WHERE user_id = ?
                """,
                [bool(is_active), bool(is_active), bool(is_active), now_iso, target_user_id],
            )
            if not is_active:
                self.revoke_all_user_sessions(
                    target_user_id, reason=f"User deactivated by {actor_username}"
                )
            self.record_audit_event(
                event_type="USER_REACTIVATED" if is_active else "USER_DEACTIVATED",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint=f"/api/admin/users/{target_user_id}/{'reactivate' if is_active else 'deactivate'}",
                method="POST",
                user_id=actor_user_id,
                username=actor_username,
                role="OWNER",
                detail={"target_user_id": target_user_id, "target_username": target["username"]},
            )
            updated = self.get_user_by_id(target_user_id)
            assert updated is not None
            return updated

    # ------------------------------------------------------------------
    # Authentication, Session Creation, Validation & Revocation
    # ------------------------------------------------------------------
    def _create_session_for_user(
        self,
        *,
        user_row: dict[str, Any],
        ip_address: str,
        user_agent: str,
    ) -> dict[str, Any]:
        session_id = f"SES-{uuid.uuid4().hex[:12].upper()}"
        raw_session_token = secrets.token_urlsafe(32)
        raw_csrf_token = secrets.token_urlsafe(32)
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        expires_iso = (now_dt + timedelta(seconds=self.settings.session_ttl_seconds)).isoformat()

        self.store._conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                session_id,
                _sha256_hex(raw_session_token),
                _sha256_hex(raw_csrf_token),
                user_row["user_id"],
                user_row["username"],
                user_row["role"],
                ip_address[:128],
                (user_agent or "unknown")[:200],
                now_iso,
                expires_iso,
                now_iso,
                None,
                None,
            ],
        )
        return {
            "session_id": session_id,
            "session_token": raw_session_token,
            "csrf_token": raw_csrf_token,
            "expires_at": expires_iso,
        }

    def login(
        self,
        *,
        username: str,
        password: str,
        ip_address: str,
        user_agent: str,
        prior_session_token: str | None = None,
    ) -> dict[str, Any]:
        uname = username.strip()
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM users WHERE lower(username) = lower(?)", [uname]
            ).fetchone()
            if not row:
                # Constant-time guard against user enumeration
                try:
                    _PASSWORD_HASHER.verify(_DUMMY_ARGON2_HASH, password or "invalid")
                except (VerifyMismatchError, InvalidHashError):
                    pass
                self.record_audit_event(
                    event_type="LOGIN_FAILED",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/login",
                    method="POST",
                    username=uname,
                    detail={"reason": "invalid_credentials"},
                )
                raise PermissionError("Invalid username or password.")

            cols = [d[0] for d in self.store._conn.description]
            user_rec = dict(zip(cols, row))

            if not user_rec["is_active"]:
                self.record_audit_event(
                    event_type="LOGIN_FAILED",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/login",
                    method="POST",
                    user_id=user_rec["user_id"],
                    username=user_rec["username"],
                    role=user_rec["role"],
                    detail={"reason": "account_deactivated"},
                )
                raise PermissionError("Account is deactivated.")

            locked_until = user_rec.get("locked_until")
            if locked_until and _parse_iso(locked_until) > now_dt:
                self.record_audit_event(
                    event_type="LOGIN_LOCKED_OUT",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/login",
                    method="POST",
                    user_id=user_rec["user_id"],
                    username=user_rec["username"],
                    role=user_rec["role"],
                    detail={"locked_until": locked_until},
                )
                raise PermissionError(f"Account temporarily locked until {locked_until}.")

            verified = False
            try:
                verified = _PASSWORD_HASHER.verify(user_rec["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                verified = False

            if not verified:
                new_fails = int(user_rec["failed_login_attempts"]) + 1
                lock_iso = None
                if new_fails >= self.settings.max_failed_logins:
                    lock_iso = (
                        now_dt + timedelta(minutes=self.settings.lockout_minutes)
                    ).isoformat()
                self.store._conn.execute(
                    """
                    UPDATE users SET
                        failed_login_attempts = ?,
                        locked_until = ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    [new_fails, lock_iso, now_iso, user_rec["user_id"]],
                )
                self.record_audit_event(
                    event_type="LOGIN_FAILED",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/login",
                    method="POST",
                    user_id=user_rec["user_id"],
                    username=user_rec["username"],
                    role=user_rec["role"],
                    detail={
                        "reason": "invalid_credentials",
                        "failed_attempts": new_fails,
                        "locked": lock_iso is not None,
                    },
                )
                raise PermissionError("Invalid username or password.")

            # Rehash automatically if Argon2 parameters upgraded
            new_hash = user_rec["password_hash"]
            if _PASSWORD_HASHER.check_needs_rehash(new_hash):
                new_hash = _PASSWORD_HASHER.hash(password)

            self.store._conn.execute(
                """
                UPDATE users SET
                    password_hash = ?,
                    failed_login_attempts = 0,
                    locked_until = NULL,
                    last_login_at = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                [new_hash, now_iso, now_iso, user_rec["user_id"]],
            )

            # Session fixation protection: revoke any caller-held session before issuing new ID/token
            if prior_session_token:
                self.revoke_session_by_token(
                    prior_session_token, reason="Session rotated on new login"
                )

            sess = self._create_session_for_user(
                user_row=user_rec,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            self.record_audit_event(
                event_type="LOGIN_SUCCESS",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint="/api/auth/login",
                method="POST",
                user_id=user_rec["user_id"],
                username=user_rec["username"],
                role=user_rec["role"],
                detail={"session_id": sess["session_id"]},
            )
            public_user = self.get_user_by_id(user_rec["user_id"])
            assert public_user is not None
            return {
                "user": {
                    **public_user,
                    "session_id": sess["session_id"],
                    "permissions": permissions_for_role(public_user["role"]),
                },
                **sess,
            }

    def create_session_after_bootstrap(
        self,
        *,
        user_record: dict[str, Any],
        ip_address: str,
        user_agent: str,
    ) -> dict[str, Any]:
        with self.store._lock:
            now_iso = utc_now_iso()
            self.store._conn.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE user_id = ?",
                [now_iso, now_iso, user_record["user_id"]],
            )
            sess = self._create_session_for_user(
                user_row=user_record, ip_address=ip_address, user_agent=user_agent
            )
            public_user = self.get_user_by_id(user_record["user_id"])
            assert public_user is not None
            return {
                "user": {
                    **public_user,
                    "session_id": sess["session_id"],
                    "permissions": permissions_for_role(public_user["role"]),
                },
                **sess,
            }

    def validate_session(self, raw_session_token: str | None) -> AuthenticatedUser | None:
        if not raw_session_token or len(raw_session_token) < 16:
            return None
        tok_hash = _sha256_hex(raw_session_token)
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        with self.store._lock:
            row = self.store._conn.execute(
                """
                SELECT
                    s.session_id, s.csrf_token_hash, s.expires_at, s.revoked_at,
                    u.user_id, u.username, u.email, u.role, u.is_active, u.must_rotate_password
                FROM sessions s
                JOIN users u ON s.user_id = u.user_id
                WHERE s.token_hash = ?
                """,
                [tok_hash],
            ).fetchone()
            if not row:
                return None

            (
                session_id,
                csrf_token_hash,
                expires_at,
                revoked_at,
                user_id,
                username,
                email,
                role,
                is_active,
                must_rotate,
            ) = row

            if revoked_at is not None:
                return None
            if _parse_iso(expires_at) <= now_dt:
                self.store._conn.execute(
                    "UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE session_id = ?",
                    [now_iso, "Session expired", session_id],
                )
                return None
            if not bool(is_active):
                self.store._conn.execute(
                    "UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE session_id = ?",
                    [now_iso, "User account inactive", session_id],
                )
                return None

            self.store._conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
                [now_iso, session_id],
            )
            return AuthenticatedUser(
                user_id=user_id,
                username=username,
                email=email,
                role=role,
                session_id=session_id,
                csrf_token_hash=csrf_token_hash,
                is_active=bool(is_active),
                must_rotate_password=bool(must_rotate),
            )

    def verify_csrf(self, user: AuthenticatedUser, header_token: str | None) -> bool:
        if not header_token:
            return False
        provided_hash = _sha256_hex(header_token.strip())
        return hmac.compare_digest(provided_hash, user.csrf_token_hash)

    def revoke_session_by_token(self, raw_token: str, reason: str = "Logged out") -> bool:
        tok_hash = _sha256_hex(raw_token)
        now_iso = utc_now_iso()
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT session_id FROM sessions WHERE token_hash = ? AND revoked_at IS NULL",
                [tok_hash],
            ).fetchone()
            if not row:
                return False
            self.store._conn.execute(
                "UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE session_id = ?",
                [now_iso, reason, row[0]],
            )
            return True

    def revoke_session_by_id(
        self,
        session_id: str,
        *,
        reason: str,
        actor: AuthenticatedUser,
        ip_address: str = "internal",
    ) -> dict[str, Any]:
        now_iso = utc_now_iso()
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT session_id, user_id, username, revoked_at FROM sessions WHERE session_id = ?",
                [session_id],
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown session_id '{session_id}'")
            sess_id, owner_uid, owner_uname, revoked_at = row
            if actor.role != "OWNER" and owner_uid != actor.user_id:
                raise PermissionError("Cannot revoke another user's session.")
            if revoked_at is None:
                self.store._conn.execute(
                    "UPDATE sessions SET revoked_at = ?, revoked_reason = ? WHERE session_id = ?",
                    [now_iso, reason, sess_id],
                )
            self.record_audit_event(
                event_type="SESSION_REVOKED",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint=f"/api/sessions/{sess_id}/revoke",
                method="POST",
                user_id=actor.user_id,
                username=actor.username,
                role=actor.role,
                detail={"revoked_session_id": sess_id, "target_username": owner_uname, "reason": reason},
            )
            return {"session_id": sess_id, "revoked": True, "reason": reason}

    def revoke_all_user_sessions(self, user_id: str, reason: str = "Revoked") -> int:
        now_iso = utc_now_iso()
        with self.store._lock:
            cnt = int(
                self.store._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND revoked_at IS NULL",
                    [user_id],
                ).fetchone()[0]
            )
            if cnt > 0:
                self.store._conn.execute(
                    """
                    UPDATE sessions SET revoked_at = ?, revoked_reason = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    [now_iso, reason, user_id],
                )
            return cnt

    def list_sessions(
        self, *, user_id: str | None = None, include_revoked: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT session_id, user_id, username, role_snapshot, ip_address, user_agent,
                   created_at, expires_at, last_seen_at, revoked_at, revoked_reason
            FROM sessions {where_sql}
            ORDER BY created_at DESC LIMIT ?
        """
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------------------------------
    # Password Change & Single-Use Time-Limited Reset Flow
    # ------------------------------------------------------------------
    def change_password(
        self,
        *,
        actor: AuthenticatedUser,
        current_password: str,
        new_password: str,
        ip_address: str,
        user_agent: str,
    ) -> dict[str, Any]:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", [actor.user_id]
            ).fetchone()
            if not row:
                raise ValueError("User not found.")
            cols = [d[0] for d in self.store._conn.description]
            user_rec = dict(zip(cols, row))

            try:
                _PASSWORD_HASHER.verify(user_rec["password_hash"], current_password)
            except (VerifyMismatchError, InvalidHashError):
                self.record_audit_event(
                    event_type="PASSWORD_CHANGE_FAILED",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/change-password",
                    method="POST",
                    user_id=actor.user_id,
                    username=actor.username,
                    role=actor.role,
                    detail={"reason": "wrong_current_password"},
                )
                raise PermissionError("Current password is incorrect.")

            if current_password == new_password:
                raise ValueError("New password must differ from current password.")

            validate_password_strength(
                new_password,
                username=actor.username,
                min_length=self.settings.min_password_length,
            )
            new_hash = _PASSWORD_HASHER.hash(new_password)
            now_iso = utc_now_iso()
            self.store._conn.execute(
                """
                UPDATE users SET
                    password_hash = ?,
                    must_rotate_password = FALSE,
                    updated_at = ?
                WHERE user_id = ?
                """,
                [new_hash, now_iso, actor.user_id],
            )
            # Revoke all previous sessions & issue a freshly rotated session
            self.revoke_all_user_sessions(actor.user_id, reason="Password changed")
            sess = self._create_session_for_user(
                user_row=user_rec,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            self.record_audit_event(
                event_type="PASSWORD_CHANGED",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint="/api/auth/change-password",
                method="POST",
                user_id=actor.user_id,
                username=actor.username,
                role=actor.role,
                detail={"rotated_session_id": sess["session_id"]},
            )
            public_user = self.get_user_by_id(actor.user_id)
            assert public_user is not None
            return {
                "user": {
                    **public_user,
                    "session_id": sess["session_id"],
                    "permissions": permissions_for_role(public_user["role"]),
                },
                **sess,
            }

    def issue_password_reset_token(
        self,
        *,
        target_user_id: str,
        actor: AuthenticatedUser,
        ttl_minutes: int = 30,
        ip_address: str = "internal",
    ) -> dict[str, Any]:
        with self.store._lock:
            target = self.get_user_by_id(target_user_id)
            if not target:
                raise ValueError(f"Unknown user_id '{target_user_id}'")
            raw_token = f"RST-{secrets.token_urlsafe(24)}"
            reset_id = f"PRESET-{uuid.uuid4().hex[:10].upper()}"
            now_dt = datetime.now(timezone.utc)
            now_iso = now_dt.isoformat()
            exp_iso = (now_dt + timedelta(minutes=max(5, min(120, ttl_minutes)))).isoformat()

            self.store._conn.execute(
                "INSERT INTO password_reset_tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    reset_id,
                    _sha256_hex(raw_token),
                    target_user_id,
                    actor.username,
                    exp_iso,
                    None,
                    now_iso,
                ],
            )
            self.record_audit_event(
                event_type="PASSWORD_RESET_ISSUED",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint=f"/api/admin/users/{target_user_id}/reset-token",
                method="POST",
                user_id=actor.user_id,
                username=actor.username,
                role=actor.role,
                detail={
                    "reset_id": reset_id,
                    "target_user_id": target_user_id,
                    "target_username": target["username"],
                    "expires_at": exp_iso,
                },
            )
            return {
                "reset_id": reset_id,
                "user_id": target_user_id,
                "username": target["username"],
                "reset_token": raw_token,
                "expires_at": exp_iso,
            }

    def confirm_password_reset(
        self,
        *,
        reset_token: str,
        new_password: str,
        ip_address: str = "internal",
    ) -> dict[str, Any]:
        tok_hash = _sha256_hex(reset_token.strip())
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        with self.store._lock:
            row = self.store._conn.execute(
                """
                SELECT r.reset_id, r.user_id, r.expires_at, r.used_at, u.username, u.role, u.is_active
                FROM password_reset_tokens r
                JOIN users u ON r.user_id = u.user_id
                WHERE r.token_hash = ?
                """,
                [tok_hash],
            ).fetchone()
            if not row:
                self.record_audit_event(
                    event_type="PASSWORD_RESET_FAILED",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/reset-password/confirm",
                    method="POST",
                    detail={"reason": "invalid_token"},
                )
                raise PermissionError("Invalid or expired password reset token.")

            reset_id, user_id, expires_at, used_at, username, role, is_active = row
            if used_at is not None or _parse_iso(expires_at) <= now_dt:
                self.record_audit_event(
                    event_type="PASSWORD_RESET_FAILED",
                    outcome="DENIED",
                    ip_address=ip_address,
                    endpoint="/api/auth/reset-password/confirm",
                    method="POST",
                    user_id=user_id,
                    username=username,
                    role=role,
                    detail={"reason": "token_used_or_expired", "reset_id": reset_id},
                )
                raise PermissionError("Password reset token has already been used or expired.")

            if not bool(is_active):
                raise PermissionError("User account is deactivated.")

            validate_password_strength(
                new_password,
                username=username,
                min_length=self.settings.min_password_length,
            )
            new_hash = _PASSWORD_HASHER.hash(new_password)
            self.store._conn.execute(
                """
                UPDATE users SET
                    password_hash = ?,
                    must_rotate_password = FALSE,
                    failed_login_attempts = 0,
                    locked_until = NULL,
                    updated_at = ?
                WHERE user_id = ?
                """,
                [new_hash, now_iso, user_id],
            )
            self.store._conn.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE reset_id = ?",
                [now_iso, reset_id],
            )
            revoked = self.revoke_all_user_sessions(user_id, reason="Password reset completed")
            self.record_audit_event(
                event_type="PASSWORD_RESET_COMPLETED",
                outcome="SUCCESS",
                ip_address=ip_address,
                endpoint="/api/auth/reset-password/confirm",
                method="POST",
                user_id=user_id,
                username=username,
                role=role,
                detail={"reset_id": reset_id, "sessions_revoked": revoked},
            )
            return {"reset_completed": True, "username": username, "sessions_revoked": revoked}
