"""Persistent thread-safe DuckDB sliding/fixed-window rate limiter."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

# (max_requests, window_seconds)
DEFAULT_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "LOGIN_IP": (10, 60),
    "LOGIN_USER": (8, 60),
    "BOOTSTRAP": (5, 300),
    "PASSWORD_RESET": (5, 300),
    "API_READ": (240, 60),
    "MUTATION_GENERAL": (60, 60),
    "AGENT_DISPATCH": (24, 60),
    "PAPER_EXECUTE": (15, 60),
    "VALIDATION_LAB": (12, 60),
    "MODEL_ROUTE": (24, 60),
    "CONFIG_CHANGE": (15, 60),
    "KILL_SWITCH": (15, 60),
    "ADMIN_WRITE": (30, 60),
}


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    bucket_class: str
    bucket_key: str
    limit: int
    remaining: int
    retry_after_seconds: int
    current_count: int


class DuckDBRateLimiter:
    """Atomic DuckDB-backed rate limiter synchronized under store._lock."""

    def __init__(
        self,
        store: DuckDBStore,
        *,
        enabled: bool = True,
        overrides: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.limits = dict(DEFAULT_RATE_LIMITS)
        if overrides:
            self.limits.update(overrides)

    def check_and_consume(
        self,
        bucket_class: str,
        identifier: str,
        *,
        now_epoch: float | None = None,
    ) -> RateLimitDecision:
        limit, window_sec = self.limits.get(bucket_class, (60, 60))
        if not self.enabled:
            return RateLimitDecision(
                allowed=True,
                bucket_class=bucket_class,
                bucket_key=f"{bucket_class}:{identifier}",
                limit=limit,
                remaining=limit,
                retry_after_seconds=0,
                current_count=0,
            )

        now = float(now_epoch if now_epoch is not None else time.time())
        key = f"{bucket_class}:{identifier}"[:180]
        now_iso = utc_now_iso()

        with self.store._lock:
            row = self.store._conn.execute(
                """
                SELECT window_start_epoch, request_count, blocked_count
                FROM rate_limit_buckets
                WHERE bucket_key = ?
                """,
                [key],
            ).fetchone()

            if row is None:
                self.store._conn.execute(
                    "INSERT INTO rate_limit_buckets VALUES (?, ?, ?, ?, ?, ?)",
                    [key, bucket_class, now, 1, 0, now_iso],
                )
                return RateLimitDecision(
                    allowed=True,
                    bucket_class=bucket_class,
                    bucket_key=key,
                    limit=limit,
                    remaining=max(0, limit - 1),
                    retry_after_seconds=0,
                    current_count=1,
                )

            win_start, req_count, blocked_count = float(row[0]), int(row[1]), int(row[2])
            if (now - win_start) >= window_sec:
                # Start new window cleanly
                self.store._conn.execute(
                    """
                    UPDATE rate_limit_buckets SET
                        window_start_epoch = ?,
                        request_count = 1,
                        updated_at = ?
                    WHERE bucket_key = ?
                    """,
                    [now, now_iso, key],
                )
                return RateLimitDecision(
                    allowed=True,
                    bucket_class=bucket_class,
                    bucket_key=key,
                    limit=limit,
                    remaining=max(0, limit - 1),
                    retry_after_seconds=0,
                    current_count=1,
                )

            if req_count >= limit:
                retry_after = max(1, int(math.ceil(window_sec - (now - win_start))))
                self.store._conn.execute(
                    """
                    UPDATE rate_limit_buckets SET
                        blocked_count = ?,
                        updated_at = ?
                    WHERE bucket_key = ?
                    """,
                    [blocked_count + 1, now_iso, key],
                )
                return RateLimitDecision(
                    allowed=False,
                    bucket_class=bucket_class,
                    bucket_key=key,
                    limit=limit,
                    remaining=0,
                    retry_after_seconds=retry_after,
                    current_count=req_count,
                )

            new_count = req_count + 1
            self.store._conn.execute(
                """
                UPDATE rate_limit_buckets SET
                    request_count = ?,
                    updated_at = ?
                WHERE bucket_key = ?
                """,
                [new_count, now_iso, key],
            )
            return RateLimitDecision(
                allowed=True,
                bucket_class=bucket_class,
                bucket_key=key,
                limit=limit,
                remaining=max(0, limit - new_count),
                retry_after_seconds=0,
                current_count=new_count,
            )
