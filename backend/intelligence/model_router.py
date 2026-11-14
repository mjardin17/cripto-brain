"""Stage 3D Cost-Aware Model Router & Strict Spending Limits.

Policy Guarantees:
- Deterministic-first routing ($0.00 cost) handles math, arbitrage, risk audits, calibration, and rules.
- Economical AI routing is strictly optional and ONLY activates when:
  1. An authorized GEMINI_API_KEY exists in server environment (never exposed to frontend).
  2. Task explicitly requests AI enrichment (`prefer_ai=True`) for non-deterministic text/research synthesis.
  3. Estimated task cost <= per_task_limit_usd ($0.01 default).
  4. Daily spend + estimated task cost <= daily_budget_usd ($0.50 default).
- When key or budget is unavailable, spends $0.00 and falls back or fails closed cleanly.
"""

from __future__ import annotations

import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

DEFAULT_PER_TASK_LIMIT_USD = 0.01
DEFAULT_DAILY_BUDGET_USD = 0.50
ECONOMICAL_MODEL_NAME = "gemini-2.5-flash-lite"
ECONOMICAL_INPUT_COST_PER_1M = 0.10
ECONOMICAL_OUTPUT_COST_PER_1M = 0.40


class CostAwareModelRouter:
    """Enforces deterministic-first model selection, token accounting, and hard spend caps."""

    def __init__(self, store: DuckDBStore) -> None:
        self.store = store
        if not self.store.get_state("model_routing_policy"):
            self.store.set_state(
                "model_routing_policy",
                {
                    "per_task_limit_usd": DEFAULT_PER_TASK_LIMIT_USD,
                    "daily_budget_usd": DEFAULT_DAILY_BUDGET_USD,
                    "deterministic_first": True,
                    "economical_model": ECONOMICAL_MODEL_NAME,
                },
            )

    def is_provider_key_configured(self) -> bool:
        """Check whether an authorized GEMINI_API_KEY is present in server env (never return the key itself)."""
        val = os.environ.get("GEMINI_API_KEY", "").strip()
        if not val or val in ("MY_GEMINI_API_KEY", "YOUR_KEY_HERE", "test", "none"):
            return False
        return True

    def get_policy(self) -> dict[str, Any]:
        pol = self.store.get_state("model_routing_policy") or {}
        return {
            "per_task_limit_usd": float(pol.get("per_task_limit_usd", DEFAULT_PER_TASK_LIMIT_USD)),
            "daily_budget_usd": float(pol.get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)),
            "deterministic_first": bool(pol.get("deterministic_first", True)),
            "economical_model": str(pol.get("economical_model", ECONOMICAL_MODEL_NAME)),
        }

    def get_daily_spend_usd(self, day_prefix: str | None = None) -> float:
        day = day_prefix or datetime.now(timezone.utc).date().isoformat()
        with self.store._lock:
            row = self.store._conn.execute(
                """
                SELECT COALESCE(SUM(actual_cost_usd), 0.0)
                FROM model_routing_log
                WHERE SUBSTR(created_at, 1, 10) = ?
                """,
                [day],
            ).fetchone()
            return round(float(row[0] or 0.0), 6) if row else 0.0

    def estimate_cost_usd(self, *, input_tokens: int, output_tokens: int) -> float:
        cost = (max(0, input_tokens) / 1_000_000.0) * ECONOMICAL_INPUT_COST_PER_1M + (
            max(0, output_tokens) / 1_000_000.0
        ) * ECONOMICAL_OUTPUT_COST_PER_1M
        return round(cost, 6)

    def route_task(
        self,
        *,
        task_id: str | None,
        agent_id: str,
        task_category: str,
        prefer_ai: bool = False,
        estimated_input_tokens: int = 800,
        estimated_output_tokens: int = 300,
        estimated_cost_override_usd: float | None = None,
    ) -> dict[str, Any]:
        """Select routing tier deterministically and atomically, enforcing $0.01/task and $0.50/day caps."""
        if estimated_cost_override_usd is not None:
            raw_override = float(estimated_cost_override_usd)
            if not math.isfinite(raw_override) or raw_override < 0.0:
                raise ValueError("estimated_cost_override_usd must be a finite non-negative number")

        cat_upper = task_category.strip().upper()
        deterministic_categories = {
            "ARBITRAGE_MATH",
            "RISK_AUDIT",
            "CALIBRATION_STATS",
            "BLOCKER_RECOVERY",
            "ORDERBOOK_NORMALIZATION",
            "MARKET_SCAN",
            "REGRESSION_TEST",
        }

        routing_id = f"RTE-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()

        # Hold the store RLock across policy read, daily spend read, cap decision, and ledger insert
        # so concurrent AI route calls cannot race past the $0.50/day budget ceiling.
        with self.store._lock:
            pol = self.get_policy()
            per_task_cap = pol["per_task_limit_usd"]
            daily_cap = pol["daily_budget_usd"]
            key_ok = self.is_provider_key_configured()
            daily_spent = self.get_daily_spend_usd()
            remaining_daily = round(max(0.0, daily_cap - daily_spent), 6)

            # 1. Deterministic-first path (always $0.00)
            if not prefer_ai or cat_upper in deterministic_categories:
                decision = {
                    "routing_id": routing_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "task_category": cat_upper,
                    "selected_tier": "DETERMINISTIC",
                    "provider": "local_deterministic",
                    "model_name": "deterministic-v1",
                    "api_key_configured": key_ok,
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reason": f"Deterministic-first execution for {cat_upper} ($0.00 cost)",
                    "status": "EXECUTED",
                    "created_at": now_iso,
                }
            else:
                token_cost = self.estimate_cost_usd(
                    input_tokens=estimated_input_tokens, output_tokens=estimated_output_tokens
                )
                est_cost = (
                    max(token_cost, round(float(estimated_cost_override_usd), 6))
                    if estimated_cost_override_usd is not None
                    else token_cost
                )
                if not key_ok:
                    decision = {
                        "routing_id": routing_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "task_category": cat_upper,
                        "selected_tier": "UNAVAILABLE",
                        "provider": "local_deterministic",
                        "model_name": "deterministic-v1",
                        "api_key_configured": False,
                        "estimated_cost_usd": est_cost,
                        "actual_cost_usd": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reason": "No authorized GEMINI_API_KEY configured; fell back cleanly with $0.00 spent",
                        "status": "FALLBACK_DETERMINISTIC",
                        "created_at": now_iso,
                    }
                elif est_cost > per_task_cap:
                    decision = {
                        "routing_id": routing_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "task_category": cat_upper,
                        "selected_tier": "BLOCKED_BUDGET",
                        "provider": "none",
                        "model_name": pol["economical_model"],
                        "api_key_configured": True,
                        "estimated_cost_usd": est_cost,
                        "actual_cost_usd": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reason": f"Blocked: estimated task cost ${est_cost:.4f} exceeds per-task cap ${per_task_cap:.2f}",
                        "status": "BLOCKED",
                        "created_at": now_iso,
                    }
                elif est_cost > remaining_daily:
                    decision = {
                        "routing_id": routing_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "task_category": cat_upper,
                        "selected_tier": "BLOCKED_BUDGET",
                        "provider": "none",
                        "model_name": pol["economical_model"],
                        "api_key_configured": True,
                        "estimated_cost_usd": est_cost,
                        "actual_cost_usd": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reason": f"Blocked: projected spend exceeds daily cap ${daily_cap:.2f} (remaining ${remaining_daily:.4f})",
                        "status": "BLOCKED",
                        "created_at": now_iso,
                    }
                else:
                    # Economical AI authorized within strict caps
                    decision = {
                        "routing_id": routing_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "task_category": cat_upper,
                        "selected_tier": "ECONOMICAL_AI",
                        "provider": "gemini",
                        "model_name": pol["economical_model"],
                        "api_key_configured": True,
                        "estimated_cost_usd": est_cost,
                        "actual_cost_usd": est_cost,
                        "input_tokens": int(max(0, estimated_input_tokens)),
                        "output_tokens": int(max(0, estimated_output_tokens)),
                        "reason": f"Authorized economical AI tier within caps (${est_cost:.6f} <= ${per_task_cap:.2f}/task)",
                        "status": "EXECUTED",
                        "created_at": now_iso,
                    }

            self.store._conn.execute(
                """
                INSERT INTO model_routing_log VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    decision["routing_id"],
                    decision["task_id"],
                    decision["agent_id"],
                    decision["task_category"],
                    decision["selected_tier"],
                    decision["provider"],
                    decision["model_name"],
                    bool(decision["api_key_configured"]),
                    float(decision["estimated_cost_usd"]),
                    float(decision["actual_cost_usd"]),
                    int(decision["input_tokens"]),
                    int(decision["output_tokens"]),
                    decision["reason"],
                    decision["status"],
                    decision["created_at"],
                ],
            )
        return decision

    def get_usage_summary(self, limit: int = 50) -> dict[str, Any]:
        pol = self.get_policy()
        key_ok = self.is_provider_key_configured()
        daily_spent = self.get_daily_spend_usd()
        remaining = round(max(0.0, pol["daily_budget_usd"] - daily_spent), 6)

        with self.store._lock:
            agg_rows = self.store._conn.execute(
                """
                SELECT selected_tier,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(input_tokens), 0) AS in_tok,
                       COALESCE(SUM(output_tokens), 0) AS out_tok,
                       COALESCE(SUM(actual_cost_usd), 0.0) AS cost_usd
                FROM model_routing_log
                GROUP BY selected_tier
                """
            ).fetchall()
            recent_rows = self.store._conn.execute(
                "SELECT * FROM model_routing_log ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]

        tier_breakdown = {
            str(r[0]): {
                "count": int(r[1] or 0),
                "input_tokens": int(r[2] or 0),
                "output_tokens": int(r[3] or 0),
                "cost_usd": round(float(r[4] or 0.0), 6),
            }
            for r in agg_rows
        }
        total_in = sum(v["input_tokens"] for v in tier_breakdown.values())
        total_out = sum(v["output_tokens"] for v in tier_breakdown.values())

        return {
            "provider": "gemini",
            "provider_status": "AVAILABLE" if key_ok else "UNAVAILABLE",
            "provenance": "LIVE" if key_ok else "UNAVAILABLE",
            "api_key_configured": key_ok,
            "api_key_exposed_to_frontend": False,
            "policy": pol,
            "daily_spent_usd": daily_spent,
            "remaining_daily_budget_usd": remaining,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "tier_breakdown": tier_breakdown,
            "recent_events": [dict(zip(cols, r)) for r in recent_rows],
        }
