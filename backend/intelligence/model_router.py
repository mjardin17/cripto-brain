"""Stage 3D+ Cost-Aware & Multi-Provider Model Router & Strict Spending Limits.

Policy Guarantees:
- Deterministic-first routing ($0.00 cost) handles math, arbitrage, risk audits, calibration, and rules.
- Multi-provider capability registry supports free/open-weight local models (Ollama),
  legitimate free-tier inference providers (Groq, OpenRouter :free, Hugging Face),
  and budget-capped Gemini economical AI ($0.01/task, $0.50/day default caps).
- Zero hardcoded API credentials and zero fabricated provider availability:
  cloud providers require valid server-side env vars; Ollama requires live HTTP /api/tags reachability.
- Rate-limit awareness & automatic ordered fallback isolate failing/unavailable providers so unrelated
  deterministic or alternative agents never stall.
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

DEFAULT_PER_TASK_LIMIT_USD = 0.01
DEFAULT_DAILY_BUDGET_USD = 0.50
ECONOMICAL_MODEL_NAME = "gemini-2.5-flash-lite"
ECONOMICAL_INPUT_COST_PER_1M = 0.10
ECONOMICAL_OUTPUT_COST_PER_1M = 0.40

INVALID_SENTINELS = {
    "",
    "MY_GEMINI_API_KEY",
    "YOUR_KEY_HERE",
    "REPLACE_ME",
    "test",
    "none",
    "null",
}

DEFAULT_FALLBACK_ORDER = [
    "ollama",
    "groq",
    "openrouter",
    "huggingface",
    "gemini",
    "local_deterministic",
]


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    kind: str  # "DETERMINISTIC" | "LOCAL_OPEN_WEIGHT" | "FREE_TIER_API" | "ECONOMICAL_PAID"
    env_vars: tuple[str, ...]
    default_model: str
    available_models: tuple[str, ...]
    capabilities: tuple[str, ...]
    cost_per_1m_input_usd: float
    cost_per_1m_output_usd: float
    rpm_limit: int | None
    daily_request_limit: int | None
    endpoint_hint: str


PROVIDER_CATALOG: dict[str, ProviderSpec] = {
    "local_deterministic": ProviderSpec(
        provider_id="local_deterministic",
        display_name="Deterministic Quantitative Engine",
        kind="DETERMINISTIC",
        env_vars=(),
        default_model="deterministic-v1",
        available_models=("deterministic-v1",),
        capabilities=(
            "math",
            "arbitrage",
            "risk_audit",
            "calibration",
            "solana_filter",
            "structured_fallback",
        ),
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        rpm_limit=None,
        daily_request_limit=None,
        endpoint_hint="in-process",
    ),
    "ollama": ProviderSpec(
        provider_id="ollama",
        display_name="Ollama Local Open-Weight",
        kind="LOCAL_OPEN_WEIGHT",
        env_vars=(),
        default_model="qwen2.5:7b",
        available_models=("qwen2.5:7b", "llama3.2:3b", "mistral:7b", "deepseek-r1:8b"),
        capabilities=("text_synthesis", "research_summary", "solana_analyst", "local_open_weight"),
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        rpm_limit=60,
        daily_request_limit=None,
        endpoint_hint="OLLAMA_BASE_URL (default http://127.0.0.1:11434)",
    ),
    "groq": ProviderSpec(
        provider_id="groq",
        display_name="Groq Open-Weight Free Tier",
        kind="FREE_TIER_API",
        env_vars=("GROQ_API_KEY",),
        default_model="llama-3.1-8b-instant",
        available_models=("llama-3.1-8b-instant", "llama-3.3-70b-versatile", "qwen/qwen3-32b"),
        capabilities=("text_synthesis", "research_summary", "solana_analyst", "fast_inference"),
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        rpm_limit=30,
        daily_request_limit=14400,
        endpoint_hint="https://api.groq.com/openai/v1/chat/completions",
    ),
    "openrouter": ProviderSpec(
        provider_id="openrouter",
        display_name="OpenRouter Free Open-Weight Pool",
        kind="FREE_TIER_API",
        env_vars=("OPENROUTER_API_KEY",),
        default_model="meta-llama/llama-3.3-70b-instruct:free",
        available_models=(
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "mistralai/mistral-small-3.1-24b-instruct:free",
        ),
        capabilities=("text_synthesis", "research_summary", "solana_analyst", "multi_model_free"),
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        rpm_limit=20,
        daily_request_limit=200,
        endpoint_hint="https://openrouter.ai/api/v1/chat/completions",
    ),
    "huggingface": ProviderSpec(
        provider_id="huggingface",
        display_name="Hugging Face Serverless Free Inference",
        kind="FREE_TIER_API",
        env_vars=("HF_API_TOKEN", "HUGGINGFACE_API_KEY"),
        default_model="Qwen/Qwen2.5-7B-Instruct",
        available_models=("Qwen/Qwen2.5-7B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3"),
        capabilities=("text_synthesis", "research_summary", "open_weight"),
        cost_per_1m_input_usd=0.0,
        cost_per_1m_output_usd=0.0,
        rpm_limit=15,
        daily_request_limit=1000,
        endpoint_hint="https://router.huggingface.co/v1/chat/completions",
    ),
    "gemini": ProviderSpec(
        provider_id="gemini",
        display_name="Google Gemini Economical Tier",
        kind="ECONOMICAL_PAID",
        env_vars=("GEMINI_API_KEY",),
        default_model=ECONOMICAL_MODEL_NAME,
        available_models=(ECONOMICAL_MODEL_NAME, "gemini-2.5-flash"),
        capabilities=("text_synthesis", "research_summary", "solana_analyst", "deep_reasoning"),
        cost_per_1m_input_usd=ECONOMICAL_INPUT_COST_PER_1M,
        cost_per_1m_output_usd=ECONOMICAL_OUTPUT_COST_PER_1M,
        rpm_limit=15,
        daily_request_limit=1500,
        endpoint_hint="https://generativelanguage.googleapis.com",
    ),
}


def _env_key_configured(env_vars: tuple[str, ...]) -> bool:
    for name in env_vars:
        val = os.environ.get(name, "").strip()
        if val and val not in INVALID_SENTINELS:
            return True
    return False


def _get_env_key(env_vars: tuple[str, ...]) -> str | None:
    for name in env_vars:
        val = os.environ.get(name, "").strip()
        if val and val not in INVALID_SENTINELS:
            return val
    return None


class CostAwareModelRouter:
    """Enforces deterministic-first selection, free/open-weight routing, rate-limit fallback, and spend caps."""

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
                    "fallback_order": list(DEFAULT_FALLBACK_ORDER),
                },
            )
        if not self.store.get_state("model_provider_runtime"):
            self.store.set_state("model_provider_runtime", {})

    def is_provider_key_configured(self, provider_id: str = "gemini") -> bool:
        """Check whether an authorized key exists in server env (never returns the key itself)."""
        spec = PROVIDER_CATALOG.get(provider_id)
        if spec is None:
            return False
        if not spec.env_vars:
            return provider_id == "local_deterministic"
        return _env_key_configured(spec.env_vars)

    def get_policy(self) -> dict[str, Any]:
        pol = self.store.get_state("model_routing_policy") or {}
        order = pol.get("fallback_order") or list(DEFAULT_FALLBACK_ORDER)
        cleaned_order = [p for p in order if p in PROVIDER_CATALOG]
        for p in DEFAULT_FALLBACK_ORDER:
            if p not in cleaned_order:
                cleaned_order.append(p)
        return {
            "per_task_limit_usd": float(pol.get("per_task_limit_usd", DEFAULT_PER_TASK_LIMIT_USD)),
            "daily_budget_usd": float(pol.get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)),
            "deterministic_first": bool(pol.get("deterministic_first", True)),
            "economical_model": str(pol.get("economical_model", ECONOMICAL_MODEL_NAME)),
            "fallback_order": cleaned_order,
        }

    def update_fallback_order(
        self,
        fallback_order: list[str],
        *,
        actor: str = "system",
        source_dataset_split: str | None = None,
        source_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        if (source_dataset_split or "").strip().upper() == "TEST":
            raise PermissionError(
                "TEST-set contamination blocked: TEST split metrics cannot be used to tune model routing"
            )
        if source_snapshot_id:
            snap = self.store.get_dataset_snapshot(source_snapshot_id)
            if snap and str(snap.get("split") or "").upper() == "TEST":
                raise PermissionError(
                    f"TEST-set contamination blocked: snapshot '{source_snapshot_id}' belongs to TEST split"
                )
        cleaned = []
        for item in fallback_order:
            p_id = str(item).strip().lower()
            if p_id in PROVIDER_CATALOG and p_id not in cleaned:
                cleaned.append(p_id)
        for p_id in DEFAULT_FALLBACK_ORDER:
            if p_id not in cleaned:
                cleaned.append(p_id)
        with self.store._lock:
            pol = self.get_policy()
            pol["fallback_order"] = cleaned
            pol["updated_by"] = actor
            pol["updated_at"] = utc_now_iso()
            self.store.set_state("model_routing_policy", pol)
        return self.get_provider_registry()

    def _get_runtime_map(self) -> dict[str, dict[str, Any]]:
        raw = self.store.get_state("model_provider_runtime")
        return dict(raw) if isinstance(raw, dict) else {}

    def _save_runtime_map(self, data: dict[str, dict[str, Any]]) -> None:
        self.store.set_state("model_provider_runtime", data)

    def record_provider_rate_limit(
        self,
        provider_id: str,
        *,
        cooldown_seconds: float = 60.0,
        reason: str = "Rate limit exceeded (HTTP 429)",
    ) -> dict[str, Any]:
        p_id = provider_id.strip().lower()
        if p_id not in PROVIDER_CATALOG:
            raise ValueError(f"Unknown provider_id '{provider_id}'")
        now_dt = datetime.now(timezone.utc)
        until_iso = (now_dt + timedelta(seconds=max(1.0, float(cooldown_seconds)))).isoformat()
        with self.store._lock:
            rt = self._get_runtime_map()
            cur = dict(rt.get(p_id, {}))
            cur.update(
                {
                    "cooldown_until": until_iso,
                    "last_error": str(reason)[:240],
                    "rate_limit_hits": int(cur.get("rate_limit_hits", 0)) + 1,
                    "updated_at": now_dt.isoformat(),
                }
            )
            rt[p_id] = cur
            self._save_runtime_map(rt)
        return self.get_provider_registry()

    def clear_provider_cooldown(self, provider_id: str) -> dict[str, Any]:
        p_id = provider_id.strip().lower()
        if p_id not in PROVIDER_CATALOG:
            raise ValueError(f"Unknown provider_id '{provider_id}'")
        with self.store._lock:
            rt = self._get_runtime_map()
            cur = dict(rt.get(p_id, {}))
            cur["cooldown_until"] = None
            cur["last_error"] = None
            cur["updated_at"] = utc_now_iso()
            rt[p_id] = cur
            self._save_runtime_map(rt)
        return self.get_provider_registry()

    def _is_in_cooldown(self, provider_id: str, runtime_map: dict[str, dict[str, Any]]) -> tuple[bool, str | None]:
        entry = runtime_map.get(provider_id) or {}
        until = entry.get("cooldown_until")
        if not until:
            return False, None
        try:
            dt = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            if dt > datetime.now(timezone.utc):
                return True, str(until)
        except ValueError:
            return False, None
        return False, None

    async def probe_providers(self, *, timeout_seconds: float = 0.8) -> dict[str, Any]:
        """Actively probe local open-weight runtime (Ollama /api/tags) and check server env keys.

        Never fabricates availability: if Ollama is not running, marks it UNAVAILABLE.
        """
        now_iso = utc_now_iso()
        ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        ollama_ok = False
        ollama_models: list[str] = []
        ollama_err: str | None = None

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                resp = await client.get(f"{ollama_base}/api/tags")
                if resp.status_code == 200:
                    payload = resp.json() or {}
                    raw_models = payload.get("models") or []
                    ollama_models = [
                        str(m.get("name"))
                        for m in raw_models
                        if isinstance(m, dict) and m.get("name")
                    ]
                    ollama_ok = True
                else:
                    ollama_err = f"HTTP {resp.status_code}"
        except Exception as exc:
            ollama_err = type(exc).__name__

        with self.store._lock:
            rt = self._get_runtime_map()
            rt["ollama"] = {
                **dict(rt.get("ollama", {})),
                "last_probe_ok": ollama_ok,
                "discovered_models": ollama_models,
                "last_probe_at": now_iso,
                "last_error": None if ollama_ok else f"Local Ollama daemon unreachable ({ollama_err})",
            }
            for pid, spec in PROVIDER_CATALOG.items():
                if pid in ("local_deterministic", "ollama"):
                    continue
                configured = _env_key_configured(spec.env_vars)
                cur = dict(rt.get(pid, {}))
                cur["last_probe_ok"] = configured
                cur["last_probe_at"] = now_iso
                if not configured:
                    cur["last_error"] = f"Missing server env key ({'/'.join(spec.env_vars)})"
                elif cur.get("last_error", "").startswith("Missing server env key"):
                    cur["last_error"] = None
                rt[pid] = cur
            self._save_runtime_map(rt)

        return self.get_provider_registry()

    def _provider_status(
        self,
        spec: ProviderSpec,
        runtime_map: dict[str, dict[str, Any]],
        daily_requests_map: dict[str, int],
    ) -> dict[str, Any]:
        pid = spec.provider_id
        rt_entry = runtime_map.get(pid) or {}
        in_cooldown, cooldown_until = self._is_in_cooldown(pid, runtime_map)
        req_today = daily_requests_map.get(pid, 0)
        daily_exhausted = (
            spec.daily_request_limit is not None and req_today >= spec.daily_request_limit
        )

        if pid == "local_deterministic":
            status = "AVAILABLE"
            configured = True
            provenance = "LIVE"
        elif pid == "ollama":
            probed_ok = bool(rt_entry.get("last_probe_ok", False))
            configured = probed_ok
            if in_cooldown:
                status = "RATE_LIMITED"
                provenance = "UNAVAILABLE"
            elif probed_ok:
                status = "AVAILABLE"
                provenance = "LIVE"
            else:
                status = "UNAVAILABLE"
                provenance = "UNAVAILABLE"
        else:
            configured = _env_key_configured(spec.env_vars)
            if not configured:
                status = "UNAVAILABLE"
                provenance = "UNAVAILABLE"
            elif in_cooldown or daily_exhausted:
                status = "RATE_LIMITED"
                provenance = "UNAVAILABLE"
            else:
                status = "AVAILABLE"
                provenance = "LIVE"

        discovered = rt_entry.get("discovered_models") or []
        models = list(discovered) if discovered else list(spec.available_models)

        return {
            "provider_id": pid,
            "display_name": spec.display_name,
            "kind": spec.kind,
            "status": status,
            "provenance": provenance,
            "api_key_configured": configured if spec.env_vars else (pid == "local_deterministic"),
            "api_key_exposed_to_frontend": False,
            "env_var_names": list(spec.env_vars),
            "default_model": models[0] if models else spec.default_model,
            "models": models,
            "capabilities": list(spec.capabilities),
            "cost_per_1m_input_usd": spec.cost_per_1m_input_usd,
            "cost_per_1m_output_usd": spec.cost_per_1m_output_usd,
            "rpm_limit": spec.rpm_limit,
            "daily_request_limit": spec.daily_request_limit,
            "requests_today": req_today,
            "cooldown_until": cooldown_until,
            "rate_limit_hits": int(rt_entry.get("rate_limit_hits", 0)),
            "last_probe_at": rt_entry.get("last_probe_at"),
            "last_error": rt_entry.get("last_error"),
            "endpoint_hint": spec.endpoint_hint,
        }

    def _get_daily_requests_by_provider(self, day_prefix: str | None = None) -> dict[str, int]:
        day = day_prefix or datetime.now(timezone.utc).date().isoformat()
        with self.store._lock:
            rows = self.store._conn.execute(
                """
                SELECT provider, COUNT(*)
                FROM model_routing_log
                WHERE SUBSTR(created_at, 1, 10) = ?
                  AND status = 'EXECUTED'
                GROUP BY provider
                """,
                [day],
            ).fetchall()
            return {str(r[0]): int(r[1] or 0) for r in rows}

    def get_provider_registry(self) -> dict[str, Any]:
        pol = self.get_policy()
        runtime_map = self._get_runtime_map()
        req_map = self._get_daily_requests_by_provider()
        providers = [
            self._provider_status(PROVIDER_CATALOG[pid], runtime_map, req_map)
            for pid in pol["fallback_order"]
            if pid in PROVIDER_CATALOG
        ]
        available_ai = [
            p["provider_id"]
            for p in providers
            if p["provider_id"] != "local_deterministic" and p["status"] == "AVAILABLE"
        ]
        return {
            "providers": providers,
            "fallback_order": pol["fallback_order"],
            "available_ai_providers": available_ai,
            "deterministic_fallback_ready": True,
            "api_key_exposed_to_frontend": False,
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

    def estimate_cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        provider_id: str = "gemini",
    ) -> float:
        spec = PROVIDER_CATALOG.get(provider_id, PROVIDER_CATALOG["gemini"])
        cost = (max(0, input_tokens) / 1_000_000.0) * spec.cost_per_1m_input_usd + (
            max(0, output_tokens) / 1_000_000.0
        ) * spec.cost_per_1m_output_usd
        return round(cost, 6)

    def route_task(
        self,
        *,
        task_id: str | None,
        agent_id: str,
        task_category: str,
        prefer_ai: bool = False,
        preferred_provider: str | None = None,
        required_capability: str | None = None,
        estimated_input_tokens: int = 800,
        estimated_output_tokens: int = 300,
        estimated_cost_override_usd: float | None = None,
    ) -> dict[str, Any]:
        """Select routing tier deterministically and atomically, with free/open-weight fallback and hard caps."""
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
            "SOLANA_FILTER",
        }

        routing_id = f"RTE-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()

        with self.store._lock:
            pol = self.get_policy()
            per_task_cap = pol["per_task_limit_usd"]
            daily_cap = pol["daily_budget_usd"]
            runtime_map = self._get_runtime_map()
            req_map = self._get_daily_requests_by_provider()
            gemini_key_ok = self.is_provider_key_configured("gemini")
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
                    "api_key_configured": gemini_key_ok,
                    "estimated_cost_usd": 0.0,
                    "actual_cost_usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reason": f"Deterministic-first execution for {cat_upper} ($0.00 cost)",
                    "status": "EXECUTED",
                    "created_at": now_iso,
                }
            else:
                # Determine ordered AI candidate providers
                order = list(pol["fallback_order"])
                if preferred_provider:
                    pref = preferred_provider.strip().lower()
                    if pref in order:
                        order = [pref] + [p for p in order if p != pref]

                # When GEMINI_API_KEY is configured without free open-weight keys/probes,
                # gemini naturally becomes first available AI provider and enforces budget caps.
                chosen_decision: dict[str, Any] | None = None
                blocked_reason: str | None = None
                blocked_cost: float = 0.0
                any_ai_available = False

                for pid in order:
                    if pid == "local_deterministic":
                        continue
                    spec = PROVIDER_CATALOG[pid]
                    if required_capability and required_capability not in spec.capabilities:
                        continue
                    st = self._provider_status(spec, runtime_map, req_map)
                    if st["status"] != "AVAILABLE":
                        continue
                    any_ai_available = True

                    if pid == "gemini":
                        token_cost = self.estimate_cost_usd(
                            input_tokens=estimated_input_tokens,
                            output_tokens=estimated_output_tokens,
                            provider_id="gemini",
                        )
                        est_cost = (
                            max(token_cost, round(float(estimated_cost_override_usd), 6))
                            if estimated_cost_override_usd is not None
                            else token_cost
                        )
                        if est_cost > per_task_cap:
                            blocked_reason = (
                                f"Blocked: estimated task cost ${est_cost:.4f} exceeds per-task cap ${per_task_cap:.2f}"
                            )
                            blocked_cost = est_cost
                            continue
                        if est_cost > remaining_daily:
                            blocked_reason = (
                                f"Blocked: projected spend exceeds daily cap ${daily_cap:.2f} (remaining ${remaining_daily:.4f})"
                            )
                            blocked_cost = est_cost
                            continue
                        chosen_decision = {
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
                        break

                    # Free open-weight or local model ($0.00 cost)
                    chosen_decision = {
                        "routing_id": routing_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "task_category": cat_upper,
                        "selected_tier": "FREE_OPEN_WEIGHT",
                        "provider": pid,
                        "model_name": st["default_model"],
                        "api_key_configured": True,
                        "estimated_cost_usd": 0.0,
                        "actual_cost_usd": 0.0,
                        "input_tokens": int(max(0, estimated_input_tokens)),
                        "output_tokens": int(max(0, estimated_output_tokens)),
                        "reason": f"Authorized free/open-weight provider '{pid}' ({st['default_model']}, $0.00 cost)",
                        "status": "EXECUTED",
                        "created_at": now_iso,
                    }
                    break

                if chosen_decision is not None:
                    decision = chosen_decision
                elif any_ai_available and blocked_reason is not None:
                    decision = {
                        "routing_id": routing_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "task_category": cat_upper,
                        "selected_tier": "BLOCKED_BUDGET",
                        "provider": "none",
                        "model_name": pol["economical_model"],
                        "api_key_configured": True,
                        "estimated_cost_usd": blocked_cost,
                        "actual_cost_usd": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reason": blocked_reason,
                        "status": "BLOCKED",
                        "created_at": now_iso,
                    }
                else:
                    est_cost = self.estimate_cost_usd(
                        input_tokens=estimated_input_tokens,
                        output_tokens=estimated_output_tokens,
                        provider_id="gemini",
                    )
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
                        "reason": "No authorized AI or local open-weight provider available; fell back cleanly with $0.00 spent",
                        "status": "FALLBACK_DETERMINISTIC",
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

    async def synthesize_with_fallback(
        self,
        *,
        task_id: str | None,
        agent_id: str,
        task_category: str,
        system_prompt: str,
        user_prompt: str,
        preferred_provider: str | None = None,
        timeout_seconds: float = 12.0,
    ) -> dict[str, Any]:
        """Attempt live text synthesis across authorized providers with real rate-limit fallback.

        Never fabricates model output if all providers are unavailable or fail.
        """
        attempted: list[dict[str, Any]] = []
        pol = self.get_policy()
        order = [p for p in pol["fallback_order"] if p != "local_deterministic"]
        if preferred_provider and preferred_provider in order:
            order = [preferred_provider] + [p for p in order if p != preferred_provider]

        for candidate_pid in order:
            route = self.route_task(
                task_id=task_id,
                agent_id=agent_id,
                task_category=task_category,
                prefer_ai=True,
                preferred_provider=candidate_pid,
                estimated_input_tokens=max(100, len(system_prompt + user_prompt) // 4),
                estimated_output_tokens=250,
            )
            if route["status"] != "EXECUTED" or route["provider"] != candidate_pid:
                attempted.append(
                    {"provider": candidate_pid, "status": route["status"], "reason": route["reason"]}
                )
                continue

            try:
                text = await self._invoke_provider_http(
                    provider_id=candidate_pid,
                    model_name=route["model_name"],
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    timeout_seconds=timeout_seconds,
                )
                return {
                    "ok": True,
                    "provider": candidate_pid,
                    "model_name": route["model_name"],
                    "routing_id": route["routing_id"],
                    "text": text,
                    "attempted": attempted,
                }
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 429:
                    self.record_provider_rate_limit(
                        candidate_pid, cooldown_seconds=60.0, reason="HTTP 429 Too Many Requests"
                    )
                attempted.append(
                    {"provider": candidate_pid, "status": f"HTTP_{code}", "reason": type(exc).__name__}
                )
            except Exception as exc:
                attempted.append(
                    {"provider": candidate_pid, "status": "ERROR", "reason": type(exc).__name__}
                )

        return {
            "ok": False,
            "provider": None,
            "model_name": None,
            "text": None,
            "error": "ALL_AI_PROVIDERS_UNAVAILABLE",
            "attempted": attempted,
        }

    async def _invoke_provider_http(
        self,
        *,
        provider_id: str,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        spec = PROVIDER_CATALOG[provider_id]
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            if provider_id == "ollama":
                base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
                resp = await client.post(
                    f"{base}/api/chat",
                    json={
                        "model": model_name,
                        "stream": False,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return str((data.get("message") or {}).get("content") or "").strip()

            if provider_id in ("groq", "openrouter", "huggingface"):
                key = _get_env_key(spec.env_vars)
                if not key:
                    raise RuntimeError(f"Missing credentials for {provider_id}")
                url_map = {
                    "groq": "https://api.groq.com/openai/v1/chat/completions",
                    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                    "huggingface": "https://router.huggingface.co/v1/chat/completions",
                }
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
                resp = await client.post(
                    url_map[provider_id],
                    headers=headers,
                    json={
                        "model": model_name,
                        "temperature": 0.1,
                        "max_tokens": 400,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                return str(((choices[0] or {}).get("message") or {}).get("content") or "").strip()

            if provider_id == "gemini":
                key = _get_env_key(spec.env_vars)
                if not key:
                    raise RuntimeError("Missing GEMINI_API_KEY")
                resp = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                    params={"key": key},
                    json={
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                cands = data.get("candidates") or []
                parts = ((cands[0] or {}).get("content") or {}).get("parts") or []
                return "".join(str(p.get("text") or "") for p in parts).strip()

            raise ValueError(f"Unsupported AI provider '{provider_id}'")

    def get_usage_summary(self, limit: int = 50) -> dict[str, Any]:
        pol = self.get_policy()
        key_ok = self.is_provider_key_configured("gemini")
        daily_spent = self.get_daily_spend_usd()
        remaining = round(max(0.0, pol["daily_budget_usd"] - daily_spent), 6)
        registry = self.get_provider_registry()
        any_ai_up = len(registry["available_ai_providers"]) > 0

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
            "provider_status": "AVAILABLE" if any_ai_up else "UNAVAILABLE",
            "provenance": "LIVE" if any_ai_up else "UNAVAILABLE",
            "api_key_configured": key_ok,
            "api_key_exposed_to_frontend": False,
            "policy": pol,
            "daily_spent_usd": daily_spent,
            "remaining_daily_budget_usd": remaining,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "tier_breakdown": tier_breakdown,
            "providers": registry["providers"],
            "available_ai_providers": registry["available_ai_providers"],
            "fallback_order": registry["fallback_order"],
            "recent_events": [dict(zip(cols, r)) for r in recent_rows],
        }
