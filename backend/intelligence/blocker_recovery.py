"""Stage 3C Autonomous Blocker Classification & Bounded Recovery Engine.

Implements the mandatory 10-step closed-loop recovery protocol:
1. Detect the failure
2. Classify the blocker
3. Search verified structured memory
4. Generate candidate recovery strategies
5. Filter strategies through permissions and safety rules (rejecting unsafe overrides)
6. Select a permitted strategy
7. Execute a bounded recovery action
8. Verify the result
9. Record the outcome as learning evidence (Stage 3B LearningEngine + blocker_events)
10. Retry safely or escalate deterministically
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from backend.intelligence.agents import AGENT_PROFILES, FORBIDDEN_GLOBAL_ACTIONS
from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

# Strategies strictly forbidden across ALL agents and ALL recovery paths
UNSAFE_RECOVERY_STRATEGIES = {
    "override_risk_gate",
    "weaken_risk_threshold",
    "bypass_authentication",
    "forge_credentials",
    "bypass_rate_limit",
    "rotate_ip_to_evade_limit",
    "bypass_kill_switch",
    "bypass_settlement_verification",
    "execute_live_order",
    "promote_unverified_memory",
    "exceed_max_recovery_attempts",
}

BOUNDED_STRATEGIES_BY_CLASS: dict[str, list[dict[str, Any]]] = {
    "HTTP_USER_AGENT_BLOCKED": [
        {
            "strategy": "use_verified_research_user_agent",
            "required_permission": "USE_VERIFIED_RESEARCH_USER_AGENT",
            "description": "Attach explicit research User-Agent per verified Phase 0 memory MEM-PHASE0-001.",
            "recoverable": True,
        },
        {
            "strategy": "bypass_authentication",
            "required_permission": "NONE",
            "description": "Attempt to bypass authentication or forge headers.",
            "recoverable": False,
        },
    ],
    "NETWORK_TRANSIENT": [
        {
            "strategy": "bounded_backoff_retry",
            "required_permission": "BOUNDED_BACKOFF_RETRY",
            "description": "Requeue task with deterministic exponential backoff within recovery cap.",
            "recoverable": True,
        },
        {
            "strategy": "exceed_max_recovery_attempts",
            "required_permission": "NONE",
            "description": "Retry indefinitely ignoring safety caps.",
            "recoverable": False,
        },
    ],
    "RATE_LIMIT": [
        {
            "strategy": "respect_rate_limit_backoff",
            "required_permission": "BOUNDED_BACKOFF_RETRY",
            "description": "Pause cleanly and requeue respecting provider rate limit cooldown.",
            "recoverable": True,
        },
        {
            "strategy": "bypass_rate_limit",
            "required_permission": "NONE",
            "description": "Circumvent provider rate limits.",
            "recoverable": False,
        },
    ],
    "STALE_DATA": [
        {
            "strategy": "refresh_market_snapshot",
            "required_permission": "REFRESH_MARKET_SNAPSHOT",
            "description": "Re-fetch fresh read-only market snapshot before re-evaluating Risk Gate.",
            "recoverable": True,
        },
        {
            "strategy": "override_risk_gate",
            "required_permission": "NONE",
            "description": "Force Risk Gate to ignore stale market data.",
            "recoverable": False,
        },
    ],
    "INSUFFICIENT_DEPTH": [
        {
            "strategy": "reduce_research_share_size",
            "required_permission": "REDUCE_RESEARCH_SHARE_SIZE",
            "description": "Conservatively halve target_shares within <=10% depth participation rule.",
            "recoverable": True,
        },
        {
            "strategy": "weaken_risk_threshold",
            "required_permission": "NONE",
            "description": "Raise 10% liquidity participation cap.",
            "recoverable": False,
        },
    ],
    "WORKER_LEASE_EXPIRED": [
        {
            "strategy": "reclaim_expired_lease_and_requeue",
            "required_permission": "SUPERVISOR_RECLAIM_LEASE",
            "description": "Supervisor revokes expired lease token and requeues task safely.",
            "recoverable": True,
        }
    ],
    "HEARTBEAT_LOST": [
        {
            "strategy": "reclaim_expired_lease_and_requeue",
            "required_permission": "SUPERVISOR_RECLAIM_LEASE",
            "description": "Supervisor marks stalled worker UNHEALTHY, clears lease, and requeues.",
            "recoverable": True,
        }
    ],
    "TASK_TIMEOUT": [
        {
            "strategy": "reclaim_expired_lease_and_requeue",
            "required_permission": "SUPERVISOR_RECLAIM_LEASE",
            "description": "Supervisor terminates timed-out attempt and requeues within retry cap.",
            "recoverable": True,
        }
    ],
    "KILL_SWITCH_ACTIVE": [
        {
            "strategy": "abort_and_escalate",
            "required_permission": "ANY",
            "description": "Halt immediately under Kill Switch; never bypass kill switch.",
            "recoverable": False,
        },
        {
            "strategy": "bypass_kill_switch",
            "required_permission": "NONE",
            "description": "Continue dispatching despite active kill switch.",
            "recoverable": False,
        },
    ],
    "RISK_GATE_DENIED": [
        {
            "strategy": "abort_and_escalate",
            "required_permission": "ANY",
            "description": "Respect fail-closed Risk Gate denial and record audit evidence.",
            "recoverable": False,
        },
        {
            "strategy": "override_risk_gate",
            "required_permission": "NONE",
            "description": "Override Risk Gate denial.",
            "recoverable": False,
        },
    ],
    "SETTLEMENT_UNVERIFIED": [
        {
            "strategy": "abort_and_escalate",
            "required_permission": "ANY",
            "description": "Halt due to unverified settlement equivalence; fail closed.",
            "recoverable": False,
        },
        {
            "strategy": "bypass_settlement_verification",
            "required_permission": "NONE",
            "description": "Skip settlement equivalence proof.",
            "recoverable": False,
        },
    ],
    "PERMISSION_DENIED": [
        {
            "strategy": "abort_and_escalate",
            "required_permission": "ANY",
            "description": "Fail closed on forbidden action or unauthorized permission request.",
            "recoverable": False,
        }
    ],
    "INTERNAL_DATA_ERROR": [
        {
            "strategy": "abort_and_escalate",
            "required_permission": "ANY",
            "description": "Escalate unexpected schema/logic defect with diagnostic evidence.",
            "recoverable": False,
        }
    ],
}


def classify_blocker(error_message: str, explicit_class: str | None = None) -> str:
    """Deterministically classify a failure string or explicit blocker code."""
    if explicit_class and explicit_class.strip():
        return explicit_class.strip().upper()
    msg = (error_message or "").lower()
    if "kill_switch" in msg or "kill switch" in msg:
        return "KILL_SWITCH_ACTIVE"
    if "403" in msg or "user-agent" in msg or "urllib" in msg:
        return "HTTP_USER_AGENT_BLOCKED"
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return "RATE_LIMIT"
    if "lease_expired" in msg or "lease expired" in msg:
        return "WORKER_LEASE_EXPIRED"
    if "heartbeat" in msg:
        return "HEARTBEAT_LOST"
    if "timeout" in msg or "timed out" in msg:
        return "TASK_TIMEOUT"
    if "stale" in msg or "data_age" in msg or "fresh_market_data" in msg:
        return "STALE_DATA"
    if "settlement" in msg or "equivalence" in msg:
        return "SETTLEMENT_UNVERIFIED"
    if "risk_gate" in msg or "risk gate" in msg or "denied" in msg and "permission" not in msg:
        return "RISK_GATE_DENIED"
    if "depth" in msg or "liquidity" in msg or "insufficient" in msg:
        return "INSUFFICIENT_DEPTH"
    if "forbidden" in msg or "permission" in msg or "unauthorized" in msg or "401" in msg:
        return "PERMISSION_DENIED"
    if "connection" in msg or "network" in msg or "502" in msg or "503" in msg or "504" in msg:
        return "NETWORK_TRANSIENT"
    return "INTERNAL_DATA_ERROR"


@dataclass(frozen=True)
class EvaluatedStrategy:
    strategy: str
    allowed: bool
    rejection_reason: str | None
    description: str
    recoverable: bool


class BlockerRecoveryEngine:
    """10-step deterministic blocker recovery manager integrated with Stage 3B LearningCore."""

    def __init__(
        self,
        store: DuckDBStore,
        memory: StructuredMemoryManager,
        learning: LearningEngine,
    ) -> None:
        self.store = store
        self.memory = memory
        self.learning = learning

    def evaluate_strategies(
        self,
        *,
        blocker_class: str,
        agent_role: str,
        recovery_attempts: int,
        max_recovery_attempts: int,
        kill_switch_active: bool,
        proposed_overrides: list[str] | None = None,
        is_supervisor: bool = False,
    ) -> list[EvaluatedStrategy]:
        """Generate candidate strategies and filter through safety rules & agent permissions."""
        profile = AGENT_PROFILES.get(agent_role.upper())
        permitted_set = set(profile.permitted_actions) if profile else set()
        if is_supervisor:
            permitted_set.add("SUPERVISOR_RECLAIM_LEASE")

        raw_candidates = list(BOUNDED_STRATEGIES_BY_CLASS.get(blocker_class, []))
        for extra in proposed_overrides or []:
            raw_candidates.append(
                {
                    "strategy": extra,
                    "required_permission": "UNKNOWN",
                    "description": f"Proposed recovery strategy '{extra}'",
                    "recoverable": True,
                }
            )

        evaluated: list[EvaluatedStrategy] = []
        seen: set[str] = set()
        for item in raw_candidates:
            name = str(item["strategy"]).strip().lower()
            if name in seen:
                continue
            seen.add(name)
            req_perm = str(item.get("required_permission", "NONE"))
            desc = str(item.get("description", ""))
            recoverable = bool(item.get("recoverable", False))

            # Safety Gate 1: Forbidden unsafe strategies ALWAYS rejected
            if name in UNSAFE_RECOVERY_STRATEGIES or name.upper() in FORBIDDEN_GLOBAL_ACTIONS:
                evaluated.append(
                    EvaluatedStrategy(
                        strategy=name,
                        allowed=False,
                        rejection_reason=f"Safety policy forbids unsafe strategy '{name}'",
                        description=desc,
                        recoverable=False,
                    )
                )
                continue

            # Safety Gate 2: Kill switch stops all retries / active recoveries
            if kill_switch_active and name != "abort_and_escalate":
                evaluated.append(
                    EvaluatedStrategy(
                        strategy=name,
                        allowed=False,
                        rejection_reason="Global Kill Switch is active; recovery retries halted",
                        description=desc,
                        recoverable=False,
                    )
                )
                continue

            # Safety Gate 3: Retry / recovery cap check
            if recoverable and recovery_attempts >= max_recovery_attempts:
                evaluated.append(
                    EvaluatedStrategy(
                        strategy=name,
                        allowed=False,
                        rejection_reason=f"Recovery cap reached ({recovery_attempts}/{max_recovery_attempts})",
                        description=desc,
                        recoverable=False,
                    )
                )
                continue

            # Safety Gate 4: Explicit role permission check
            if req_perm != "ANY" and req_perm not in permitted_set:
                evaluated.append(
                    EvaluatedStrategy(
                        strategy=name,
                        allowed=False,
                        rejection_reason=f"Role '{agent_role}' lacks Supervisor permission '{req_perm}'",
                        description=desc,
                        recoverable=False,
                    )
                )
                continue

            evaluated.append(
                EvaluatedStrategy(
                    strategy=name,
                    allowed=True,
                    rejection_reason=None,
                    description=desc,
                    recoverable=recoverable,
                )
            )

        if not any(e.allowed for e in evaluated):
            evaluated.append(
                EvaluatedStrategy(
                    strategy="abort_and_escalate",
                    allowed=True,
                    rejection_reason=None,
                    description="Default fail-closed escalation when no safe recovery is permitted.",
                    recoverable=False,
                )
            )
        return evaluated

    def handle_blocker(
        self,
        *,
        task: dict[str, Any],
        error_message: str,
        explicit_class: str | None = None,
        worker_id: str | None = None,
        proposed_strategies: list[str] | None = None,
        is_supervisor: bool = False,
        reviewer_id: str = "learning_agent",
    ) -> dict[str, Any]:
        """Execute the full 10-step autonomous blocker recovery pipeline."""
        blocker_id = f"BLK-{uuid.uuid4().hex[:10].upper()}"
        task_id = str(task["task_id"])
        agent_role = str(task["assigned_agent"]).upper()
        recovery_attempts = int(task.get("recovery_attempts", 0))
        max_recovery_attempts = int(task.get("max_recovery_attempts", 3))
        kill_switch_active = self.store.is_kill_switch_active()

        # Step 2: Classify blocker
        blocker_class = classify_blocker(error_message, explicit_class=explicit_class)

        # Step 3: Search verified structured memory (with fallback keywords)
        memory_query = f"{blocker_class.replace('_', ' ')} {error_message}"
        mem_hits = self.memory.search_relevant(
            memory_query,
            statuses=["VERIFIED_LESSON", "CANDIDATE"],
            limit=5,
        )
        hit_summaries = [
            {
                "memory_id": m["memory_id"],
                "title": m["title"],
                "status": m["status"],
                "relevance_score": m.get("relevance_score", 0.0),
            }
            for m in mem_hits
        ]

        # Steps 4 & 5: Generate and filter strategies through permissions & safety rules
        evaluated = self.evaluate_strategies(
            blocker_class=blocker_class,
            agent_role=agent_role,
            recovery_attempts=recovery_attempts,
            max_recovery_attempts=max_recovery_attempts,
            kill_switch_active=kill_switch_active,
            proposed_overrides=proposed_strategies,
            is_supervisor=is_supervisor,
        )

        # Step 6: Select highest-priority allowed strategy
        allowed_recoverable = [e for e in evaluated if e.allowed and e.recoverable]
        selected = allowed_recoverable[0] if allowed_recoverable else next(e for e in evaluated if e.allowed)

        # Step 7 & 8: Execute bounded action & verify safety invariants
        payload_patch: dict[str, Any] = {}
        should_retry = False
        outcome = "ESCALATED"
        recovery_action = selected.strategy
        verification_detail = ""

        if selected.recoverable and not kill_switch_active:
            should_retry = True
            outcome = "RETRY_SCHEDULED" if selected.strategy == "bounded_backoff_retry" else "RECOVERED"
            if selected.strategy == "reduce_research_share_size":
                cur_shares = float((task.get("payload") or {}).get("target_shares", 25.0))
                new_shares = max(1.0, round(cur_shares * 0.5, 2))
                payload_patch["target_shares"] = new_shares
                verification_detail = f"Reduced research target_shares from {cur_shares} to {new_shares}; risk policy unchanged."
            elif selected.strategy == "use_verified_research_user_agent":
                payload_patch["use_research_user_agent"] = True
                verification_detail = "Enabled explicit httpx research User-Agent per verified memory MEM-PHASE0-001."
            elif selected.strategy == "respect_rate_limit_backoff":
                backoff_ms = min(30_000, 1000 * (2 ** recovery_attempts))
                payload_patch["cooldown_ms"] = backoff_ms
                verification_detail = f"Applied deterministic {backoff_ms}ms rate-limit cooldown without bypassing limit."
            elif selected.strategy == "refresh_market_snapshot":
                payload_patch["force_refresh_snapshot"] = True
                verification_detail = "Scheduled fresh read-only market snapshot fetch prior to Risk Gate check."
            elif selected.strategy == "reclaim_expired_lease_and_requeue":
                verification_detail = "Revoked expired/stalled lease token and reset task for safe dispatch."
            else:
                backoff_ms = min(10_000, 250 * (2 ** recovery_attempts))
                payload_patch["backoff_ms"] = backoff_ms
                verification_detail = f"Scheduled bounded retry #{recovery_attempts + 1}/{max_recovery_attempts} (backoff={backoff_ms}ms)."
        else:
            rejected_unsafe = [e for e in evaluated if not e.allowed and e.strategy in UNSAFE_RECOVERY_STRATEGIES]
            if rejected_unsafe and proposed_strategies:
                outcome = "REJECTED_UNSAFE"
                verification_detail = "; ".join(f"{e.strategy}: {e.rejection_reason}" for e in rejected_unsafe)
            else:
                outcome = "ESCALATED"
                verification_detail = (
                    f"Escalated fail-closed ({blocker_class}): "
                    + (
                        "Kill switch active"
                        if kill_switch_active
                        else (
                            f"recovery attempts exhausted ({recovery_attempts}/{max_recovery_attempts})"
                            if recovery_attempts >= max_recovery_attempts
                            else "non-recoverable blocker under permanent safety policy"
                        )
                    )
                )

        # Step 9: Record outcome as learning evidence via Stage 3B LearningEngine
        effective_reviewer = reviewer_id if reviewer_id.lower() != agent_role.lower() else "supervisor_reviewer"
        cycle = self.learning.record_recovery_learning(
            blocker_id=blocker_id,
            agent_id=agent_role.lower(),
            subject_key=f"blocker:{blocker_class.lower()}",
            strategy_name=selected.strategy,
            succeeded=should_retry,
            input_fixture={
                "task_id": task_id,
                "blocker_class": blocker_class,
                "recovery_attempt": recovery_attempts + 1,
                "kill_switch_active": kill_switch_active,
            },
            expected_invariant={
                "live_trading_enabled": False,
                "risk_gate_overridden": False,
                "recovered": True,
            },
            reviewer_id=effective_reviewer,
        )

        # Persist blocker event row
        cand_json = json.dumps(
            [
                {
                    "strategy": e.strategy,
                    "allowed": e.allowed,
                    "rejection_reason": e.rejection_reason,
                    "recoverable": e.recoverable,
                }
                for e in evaluated
            ]
        )
        now_iso = utc_now_iso()
        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO blocker_events VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    blocker_id,
                    task_id,
                    agent_role,
                    worker_id,
                    blocker_class,
                    error_message[:500],
                    json.dumps(hit_summaries),
                    cand_json,
                    selected.strategy,
                    recovery_action,
                    verification_detail,
                    outcome,
                    cycle["cycle_id"],
                    cycle.get("regression_case_id"),
                    now_iso,
                ],
            )

        return {
            "blocker_id": blocker_id,
            "task_id": task_id,
            "blocker_class": blocker_class,
            "memory_hits": hit_summaries,
            "candidate_strategies": json.loads(cand_json),
            "selected_strategy": selected.strategy,
            "should_retry": should_retry,
            "outcome": outcome,
            "payload_patch": payload_patch,
            "verification_detail": verification_detail,
            "learning_cycle_id": cycle["cycle_id"],
            "regression_case_id": cycle.get("regression_case_id"),
            "created_at": now_iso,
        }

    def list_blockers(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM blocker_events ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["memory_hits"] = json.loads(d.pop("memory_hits_json", "[]"))
                d["candidate_strategies"] = json.loads(d.pop("candidate_strategies_json", "[]"))
                out.append(d)
            return out
