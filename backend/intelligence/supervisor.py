"""Stage 3C Persistent Supervisor, Task Queue, Worker Pool & Dynamic Scaling.

Responsibilities:
- Persistent task lifecycle (`PENDING`, `QUEUED`, `LEASED`, `RUNNING`, `BLOCKED`, `COMPLETED`, `FAILED`, `ESCALATED`, `CANCELLED`)
- Deterministic deduplication (`dedup_key`) preventing duplicate active tasks
- Dependency ordering and fail-closed cascading on upstream failures
- Strict lease & fencing-token ownership + worker heartbeats
- Expired lease, heartbeat loss, and task timeout detection & recovery
- Bounded dynamic worker scaling (default global concurrency: 4, hard cap: 8)
- Bounded retry / recovery caps (default max attempts: 3) -> deterministic escalation
- Kill Switch enforcement halting new task dispatch immediately
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.intelligence.agents import (
    AGENT_PROFILES,
    VALID_AGENT_ROLES,
    AgentExecutionContext,
    execute_agent_task,
)
from backend.intelligence.blocker_recovery import BlockerRecoveryEngine
from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.risk_gate.engine import DeterministicRiskGate
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

ACTIVE_NON_TERMINAL_STATUSES = ("PENDING", "QUEUED", "LEASED", "RUNNING", "BLOCKED")
TERMINAL_FAILURE_STATUSES = ("FAILED", "ESCALATED", "CANCELLED")


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


class SupervisorOrchestrator:
    """Persistent Supervisor controlling 5 specialized agents, leases, scaling, and recovery."""

    DEFAULT_MAX_CONCURRENCY = 4
    HARD_MAX_CONCURRENCY = 8
    DEFAULT_MAX_RECOVERY_ATTEMPTS = 3
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_LEASE_SECONDS = 15.0
    DEFAULT_HEARTBEAT_GRACE_SECONDS = 20.0

    def __init__(
        self,
        *,
        store: DuckDBStore,
        risk_gate: DeterministicRiskGate,
        memory: StructuredMemoryManager,
        learning: LearningEngine,
        scanner: Any = None,
        cross_scanner: Any = None,
    ) -> None:
        self.store = store
        self.risk_gate = risk_gate
        self.memory = memory
        self.learning = learning
        self.scanner = scanner
        self.cross_scanner = cross_scanner
        self.blocker_engine = BlockerRecoveryEngine(store=store, memory=memory, learning=learning)

        # Ensure supervisor policy persisted across restarts
        policy = self.store.get_state("supervisor_policy")
        if not policy:
            self.store.set_state(
                "supervisor_policy",
                {
                    "max_concurrency": self.DEFAULT_MAX_CONCURRENCY,
                    "hard_max_concurrency": self.HARD_MAX_CONCURRENCY,
                    "max_recovery_attempts": self.DEFAULT_MAX_RECOVERY_ATTEMPTS,
                    "max_retries": self.DEFAULT_MAX_RETRIES,
                    "lease_seconds": self.DEFAULT_LEASE_SECONDS,
                    "heartbeat_grace_seconds": self.DEFAULT_HEARTBEAT_GRACE_SECONDS,
                },
            )

    def get_policy(self) -> dict[str, Any]:
        pol = self.store.get_state("supervisor_policy") or {}
        return {
            "max_concurrency": int(pol.get("max_concurrency", self.DEFAULT_MAX_CONCURRENCY)),
            "hard_max_concurrency": self.HARD_MAX_CONCURRENCY,
            "max_recovery_attempts": int(pol.get("max_recovery_attempts", self.DEFAULT_MAX_RECOVERY_ATTEMPTS)),
            "max_retries": int(pol.get("max_retries", self.DEFAULT_MAX_RETRIES)),
            "lease_seconds": float(pol.get("lease_seconds", self.DEFAULT_LEASE_SECONDS)),
            "heartbeat_grace_seconds": float(
                pol.get("heartbeat_grace_seconds", self.DEFAULT_HEARTBEAT_GRACE_SECONDS)
            ),
        }

    def set_max_concurrency(self, max_concurrency: int) -> dict[str, Any]:
        bounded = max(1, min(self.HARD_MAX_CONCURRENCY, int(max_concurrency)))
        pol = self.get_policy()
        pol["max_concurrency"] = bounded
        self.store.set_state("supervisor_policy", pol)
        self.log_activity(
            actor="supervisor",
            agent_role="SUPERVISOR",
            event_type="CONCURRENCY_UPDATED",
            summary=f"Bounded max_concurrency set to {bounded} (hard cap {self.HARD_MAX_CONCURRENCY})",
            detail={"max_concurrency": bounded},
        )
        return pol

    def log_activity(
        self,
        *,
        actor: str,
        agent_role: str,
        event_type: str,
        summary: str,
        task_id: str | None = None,
        worker_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        act_id = f"ACT-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()
        with self.store._lock:
            self.store._conn.execute(
                "INSERT INTO agent_activity_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    act_id,
                    actor,
                    agent_role,
                    task_id,
                    worker_id,
                    event_type,
                    summary[:300],
                    json.dumps(detail or {}),
                    now_iso,
                ],
            )
        return act_id

    def list_activity(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM agent_activity_log ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["detail"] = json.loads(d.pop("detail_json", "{}"))
                out.append(d)
            return out

    # ------------------------------------------------------------------
    # Row Deserializers
    # ------------------------------------------------------------------
    def _row_to_task(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["payload"] = json.loads(d.pop("payload_json", "{}"))
        d["result"] = json.loads(d.pop("result_json", "{}"))
        d["depends_on"] = json.loads(d.pop("depends_on_json", "[]"))
        return d

    def _row_to_worker(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["permitted_actions"] = json.loads(d.pop("permitted_actions_json", "[]"))
        return d

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?",
                [task_id],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_task(cols, row)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        assigned_agent: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status.upper())
        if assigned_agent:
            clauses.append("assigned_agent = ?")
            params.append(assigned_agent.upper())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_tasks {where_sql} ORDER BY priority ASC, created_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_task(cols, r) for r in rows]

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM worker_registry WHERE worker_id = ?",
                [worker_id],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_worker(cols, row)

    def list_workers(self, include_stopped: bool = False) -> list[dict[str, Any]]:
        with self.store._lock:
            if include_stopped:
                rows = self.store._conn.execute(
                    "SELECT * FROM worker_registry ORDER BY spawned_at ASC"
                ).fetchall()
            else:
                rows = self.store._conn.execute(
                    "SELECT * FROM worker_registry WHERE status != 'STOPPED' ORDER BY spawned_at ASC"
                ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_worker(cols, r) for r in rows]

    # ------------------------------------------------------------------
    # Task Submission, Deduplication & Dependencies
    # ------------------------------------------------------------------
    def submit_task(
        self,
        *,
        task_type: str,
        assigned_agent: str,
        payload: dict[str, Any] | None = None,
        priority: int = 5,
        depends_on: list[str] | None = None,
        dedup_key: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int | None = None,
        max_recovery_attempts: int | None = None,
        created_by: str = "operator",
    ) -> dict[str, Any]:
        role = assigned_agent.strip().upper()
        if role not in VALID_AGENT_ROLES:
            raise ValueError(f"Unsupported agent role '{assigned_agent}'. Valid: {VALID_AGENT_ROLES}")

        t_type = task_type.strip().upper()
        payload_obj = dict(payload or {})
        deps = [str(d).strip() for d in (depends_on or []) if str(d).strip()]
        pol = self.get_policy()
        eff_max_retries = int(max_retries if max_retries is not None else pol["max_retries"])
        eff_max_rec = int(
            max_recovery_attempts if max_recovery_attempts is not None else pol["max_recovery_attempts"]
        )

        if dedup_key and dedup_key.strip():
            d_key = dedup_key.strip().lower()[:140]
        else:
            canon = json.dumps(
                {"task_type": t_type, "assigned_agent": role, "payload": payload_obj, "depends_on": deps},
                sort_keys=True,
            )
            digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
            d_key = f"{t_type.lower()}:{role.lower()}:{digest}"

        with self.store._lock:
            # Duplicate task prevention for active non-terminal tasks
            existing_row = self.store._conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE dedup_key = ?
                  AND status IN ('PENDING', 'QUEUED', 'LEASED', 'RUNNING', 'BLOCKED')
                LIMIT 1
                """,
                [d_key],
            ).fetchone()
            if existing_row:
                cols = [d[0] for d in self.store._conn.description]
                existing_task = self._row_to_task(cols, existing_row)
                self.log_activity(
                    actor=created_by,
                    agent_role=role,
                    task_id=existing_task["task_id"],
                    event_type="DUPLICATE_TASK_PREVENTED",
                    summary=f"Prevented duplicate active task for dedup_key '{d_key}'",
                    detail={"dedup_key": d_key},
                )
                return {
                    "created": False,
                    "duplicate_prevented": True,
                    "task": existing_task,
                }

            # Dependency validation
            initial_status = "QUEUED"
            escalation_reason = None
            if deps:
                dep_statuses: list[str] = []
                for dep_id in deps:
                    dep = self.get_task(dep_id)
                    if not dep:
                        raise ValueError(f"Dependency task_id '{dep_id}' does not exist")
                    dep_statuses.append(dep["status"])
                if any(s in TERMINAL_FAILURE_STATUSES or s == "BLOCKED" for s in dep_statuses):
                    initial_status = "BLOCKED"
                    escalation_reason = "Upstream dependency failed, escalated, or cancelled"
                elif all(s == "COMPLETED" for s in dep_statuses):
                    initial_status = "QUEUED"
                else:
                    initial_status = "PENDING"

            task_id = f"TASK-{uuid.uuid4().hex[:10].upper()}"
            now_iso = utc_now_iso()
            self.store._conn.execute(
                """
                INSERT INTO agent_tasks VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    task_id,
                    d_key,
                    t_type,
                    role,
                    initial_status,
                    max(1, min(10, int(priority))),
                    json.dumps(payload_obj),
                    json.dumps({}),
                    json.dumps(deps),
                    None,
                    None,
                    None,
                    None,
                    float(max(0.05, timeout_seconds)),
                    None,
                    0,
                    eff_max_retries,
                    0,
                    eff_max_rec,
                    None,
                    escalation_reason,
                    created_by,
                    now_iso,
                    now_iso,
                ],
            )
            self.log_activity(
                actor=created_by,
                agent_role=role,
                task_id=task_id,
                event_type="TASK_SUBMITTED",
                summary=f"Submitted {t_type} -> {role} (status={initial_status}, deps={len(deps)})",
                detail={"dedup_key": d_key, "depends_on": deps},
            )
            created = self.get_task(task_id)
            assert created is not None
            return {
                "created": True,
                "duplicate_prevented": False,
                "task": created,
            }

    def _reconcile_dependents_on_parent_change(self, parent_task_id: str, parent_status: str) -> None:
        now_iso = utc_now_iso()
        rows = self.store._conn.execute(
            "SELECT * FROM agent_tasks WHERE status IN ('PENDING', 'QUEUED')"
        ).fetchall()
        cols = [d[0] for d in self.store._conn.description]
        for r in rows:
            child = self._row_to_task(cols, r)
            deps = child["depends_on"]
            if parent_task_id not in deps:
                continue
            if parent_status in TERMINAL_FAILURE_STATUSES or parent_status == "BLOCKED":
                self.store._conn.execute(
                    """
                    UPDATE agent_tasks SET
                        status = 'BLOCKED',
                        escalation_reason = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    [
                        f"Fail-closed: dependency {parent_task_id} ended with status {parent_status}",
                        now_iso,
                        child["task_id"],
                    ],
                )
            elif parent_status == "COMPLETED" and child["status"] == "PENDING":
                dep_rows = [self.get_task(d) for d in deps]
                if all(d is not None and d["status"] == "COMPLETED" for d in dep_rows):
                    self.store._conn.execute(
                        "UPDATE agent_tasks SET status = 'QUEUED', updated_at = ? WHERE task_id = ?",
                        [now_iso, child["task_id"]],
                    )

    # ------------------------------------------------------------------
    # Worker Registry & Dynamic Scaling (Bounded by max_concurrency)
    # ------------------------------------------------------------------
    def spawn_worker(self, agent_role: str) -> dict[str, Any]:
        role = agent_role.strip().upper()
        if role not in AGENT_PROFILES:
            raise ValueError(f"Unsupported agent role '{agent_role}'")
        if self.store.is_kill_switch_active():
            raise RuntimeError("Kill Switch is active: cannot spawn new active workers")

        pol = self.get_policy()
        with self.store._lock:
            active_count = self.store._conn.execute(
                "SELECT COUNT(*) FROM worker_registry WHERE status IN ('IDLE', 'BUSY')"
            ).fetchone()[0]
            if int(active_count) >= pol["max_concurrency"]:
                raise RuntimeError(
                    f"Concurrency bound reached: {active_count}/{pol['max_concurrency']} active workers"
                )

            worker_id = f"WRK-{role[:4]}-{uuid.uuid4().hex[:6].upper()}"
            now_iso = utc_now_iso()
            perms = list(AGENT_PROFILES[role].permitted_actions)
            self.store._conn.execute(
                "INSERT INTO worker_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    worker_id,
                    role,
                    "IDLE",
                    None,
                    json.dumps(perms),
                    now_iso,
                    0,
                    0,
                    now_iso,
                    now_iso,
                ],
            )
            self.log_activity(
                actor="supervisor",
                agent_role=role,
                worker_id=worker_id,
                event_type="WORKER_SPAWNED",
                summary=f"Spawned worker {worker_id} for {role}",
            )
            w = self.get_worker(worker_id)
            assert w is not None
            return w

    def stop_worker(self, worker_id: str, reason: str = "Scaled down cleanly") -> dict[str, Any]:
        with self.store._lock:
            w = self.get_worker(worker_id)
            if not w:
                raise ValueError(f"Unknown worker_id '{worker_id}'")
            if w["status"] == "BUSY" and w.get("current_task_id"):
                raise RuntimeError(f"Cannot stop BUSY worker '{worker_id}' holding task '{w['current_task_id']}'")
            now_iso = utc_now_iso()
            self.store._conn.execute(
                "UPDATE worker_registry SET status = 'STOPPED', updated_at = ? WHERE worker_id = ?",
                [now_iso, worker_id],
            )
            self.log_activity(
                actor="supervisor",
                agent_role=w["agent_role"],
                worker_id=worker_id,
                event_type="WORKER_STOPPED",
                summary=f"Stopped worker {worker_id}: {reason}",
            )
            stopped = self.get_worker(worker_id)
            assert stopped is not None
            return stopped

    def scale_workers(self, target_concurrency: int | None = None) -> dict[str, Any]:
        """Dynamically scale workers up to queue demand within bounded concurrency (default 4)."""
        pol = self.get_policy()
        cap = pol["max_concurrency"]
        if target_concurrency is not None:
            cap = max(0, min(pol["max_concurrency"], int(target_concurrency)))

        kill_active = self.store.is_kill_switch_active()
        spawned: list[str] = []
        stopped: list[str] = []

        with self.store._lock:
            # Always scale down idle excess when kill switch is active or cap shrunk
            active_workers = self.store._conn.execute(
                "SELECT worker_id, agent_role, status FROM worker_registry WHERE status IN ('IDLE', 'BUSY') ORDER BY spawned_at ASC"
            ).fetchall()

            if kill_active:
                for w_id, _, st in active_workers:
                    if st == "IDLE":
                        self.stop_worker(w_id, reason="Kill Switch active")
                        stopped.append(w_id)
                return {
                    "kill_switch_active": True,
                    "halted": True,
                    "cap": cap,
                    "spawned": spawned,
                    "stopped": stopped,
                }

            # Demand per role from QUEUED tasks
            demand_rows = self.store._conn.execute(
                "SELECT assigned_agent, COUNT(*) FROM agent_tasks WHERE status = 'QUEUED' GROUP BY assigned_agent"
            ).fetchall()
            demand_by_role = {str(r): int(c) for r, c in demand_rows}
            total_demand = sum(demand_by_role.values())

            current_by_role: dict[str, list[tuple[str, str]]] = {}
            for w_id, role, st in active_workers:
                current_by_role.setdefault(str(role), []).append((str(w_id), str(st)))

            # 1. Scale down IDLE workers exceeding role demand or concurrency cap
            total_active = len(active_workers)
            for role, w_list in list(current_by_role.items()):
                role_need = demand_by_role.get(role, 0)
                idle_ids = [w_id for w_id, st in w_list if st == "IDLE"]
                busy_count = sum(1 for _, st in w_list if st == "BUSY")
                allowed_total_for_role = role_need
                excess = max(0, (busy_count + len(idle_ids)) - allowed_total_for_role)
                if total_demand == 0:
                    excess = len(idle_ids)
                for w_id in idle_ids[:excess]:
                    self.stop_worker(w_id, reason="Queue demand satisfied")
                    stopped.append(w_id)
                    total_active -= 1
                    current_by_role[role] = [(x, s) for x, s in current_by_role[role] if x != w_id]

            # If still over cap due to shrinking target_concurrency, stop extra IDLE workers
            if total_active > cap:
                for role, w_list in list(current_by_role.items()):
                    for w_id, st in list(w_list):
                        if total_active <= cap:
                            break
                        if st == "IDLE" and w_id not in stopped:
                            self.stop_worker(w_id, reason="Concurrency cap enforcement")
                            stopped.append(w_id)
                            total_active -= 1

            # 2. Scale up up to cap matching queued demand per role
            for role, needed in sorted(demand_by_role.items(), key=lambda x: x[1], reverse=True):
                if total_active >= cap:
                    break
                existing_role_workers = len(current_by_role.get(role, []))
                to_spawn = min(needed - existing_role_workers, cap - total_active)
                for _ in range(max(0, to_spawn)):
                    w = self.spawn_worker(role)
                    spawned.append(w["worker_id"])
                    total_active += 1

        return {
            "kill_switch_active": False,
            "halted": False,
            "cap": cap,
            "spawned": spawned,
            "stopped": stopped,
            "active_workers_count": len(self.list_workers()),
        }

    # ------------------------------------------------------------------
    # Leases, Strict Fencing Ownership, Heartbeats & Completion
    # ------------------------------------------------------------------
    def claim_next_task(
        self,
        worker_id: str,
        *,
        lease_duration_seconds: float | None = None,
        now_iso: str | None = None,
    ) -> dict[str, Any] | None:
        if self.store.is_kill_switch_active():
            self.log_activity(
                actor="supervisor",
                agent_role="SUPERVISOR",
                worker_id=worker_id,
                event_type="DISPATCH_BLOCKED_KILL_SWITCH",
                summary="Halted task claim because Global Kill Switch is active",
            )
            return None

        pol = self.get_policy()
        lease_sec = float(lease_duration_seconds or pol["lease_seconds"])
        now_dt = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        lease_exp_str = (now_dt + timedelta(seconds=lease_sec)).isoformat()

        with self.store._lock:
            worker = self.get_worker(worker_id)
            if not worker:
                raise ValueError(f"Unknown worker_id '{worker_id}'")
            if worker["status"] != "IDLE" or worker.get("current_task_id"):
                raise RuntimeError(
                    f"Worker '{worker_id}' is '{worker['status']}' and cannot claim another task"
                )

            candidate_rows = self.store._conn.execute(
                """
                SELECT * FROM agent_tasks
                WHERE status = 'QUEUED' AND assigned_agent = ?
                ORDER BY priority ASC, created_at ASC
                """,
                [worker["agent_role"]],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]

            selected_task: dict[str, Any] | None = None
            for r in candidate_rows:
                cand = self._row_to_task(cols, r)
                deps_ok = True
                for dep_id in cand["depends_on"]:
                    dep = self.get_task(dep_id)
                    if not dep or dep["status"] != "COMPLETED":
                        deps_ok = False
                        break
                if deps_ok:
                    selected_task = cand
                    break

            if not selected_task:
                return None

            lease_token = f"LEASE-{uuid.uuid4().hex[:12].upper()}"
            new_attempts = int(selected_task["attempt_count"]) + 1
            self.store._conn.execute(
                """
                UPDATE agent_tasks SET
                    status = 'LEASED',
                    worker_id = ?,
                    lease_token = ?,
                    lease_expires_at = ?,
                    last_heartbeat_at = ?,
                    attempt_count = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                [
                    worker_id,
                    lease_token,
                    lease_exp_str,
                    now_str,
                    new_attempts,
                    now_str,
                    selected_task["task_id"],
                ],
            )
            self.store._conn.execute(
                """
                UPDATE worker_registry SET
                    status = 'BUSY',
                    current_task_id = ?,
                    last_heartbeat_at = ?,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                [selected_task["task_id"], now_str, now_str, worker_id],
            )
            self.log_activity(
                actor=worker_id,
                agent_role=worker["agent_role"],
                task_id=selected_task["task_id"],
                worker_id=worker_id,
                event_type="TASK_LEASED",
                summary=f"Leased {selected_task['task_id']} (attempt #{new_attempts}, token={lease_token})",
            )
            return self.get_task(selected_task["task_id"])

    def _assert_lease_ownership(
        self, task_id: str, worker_id: str, lease_token: str, allowed_statuses: tuple[str, ...]
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Unknown task_id '{task_id}'")
        if task["status"] not in allowed_statuses:
            raise PermissionError(
                f"Task '{task_id}' status '{task['status']}' not valid for lease holder operation"
            )
        if task.get("worker_id") != worker_id or task.get("lease_token") != lease_token:
            raise PermissionError(
                f"Ownership denied: worker '{worker_id}' / token '{lease_token}' does not own task '{task_id}'"
            )
        return task

    def start_task(
        self, task_id: str, *, worker_id: str, lease_token: str, now_iso: str | None = None
    ) -> dict[str, Any]:
        now_str = now_iso or utc_now_iso()
        with self.store._lock:
            task = self._assert_lease_ownership(task_id, worker_id, lease_token, ("LEASED",))
            self.store._conn.execute(
                """
                UPDATE agent_tasks SET
                    status = 'RUNNING',
                    started_at = ?,
                    last_heartbeat_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                [now_str, now_str, now_str, task_id],
            )
            self.log_activity(
                actor=worker_id,
                agent_role=task["assigned_agent"],
                task_id=task_id,
                worker_id=worker_id,
                event_type="TASK_STARTED",
                summary=f"Worker {worker_id} started {task_id}",
            )
            started = self.get_task(task_id)
            assert started is not None
            return started

    def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        extend_lease_seconds: float | None = None,
        now_iso: str | None = None,
    ) -> dict[str, Any]:
        pol = self.get_policy()
        lease_sec = float(extend_lease_seconds or pol["lease_seconds"])
        now_dt = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        new_exp_str = (now_dt + timedelta(seconds=lease_sec)).isoformat()

        with self.store._lock:
            task = self._assert_lease_ownership(task_id, worker_id, lease_token, ("LEASED", "RUNNING"))
            self.store._conn.execute(
                """
                UPDATE agent_tasks SET
                    last_heartbeat_at = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                [now_str, new_exp_str, now_str, task_id],
            )
            self.store._conn.execute(
                """
                UPDATE worker_registry SET
                    last_heartbeat_at = ?,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                [now_str, now_str, worker_id],
            )
            updated = self.get_task(task_id)
            assert updated is not None
            return updated

    def complete_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now_str = utc_now_iso()
        with self.store._lock:
            task = self._assert_lease_ownership(task_id, worker_id, lease_token, ("LEASED", "RUNNING"))
            self.store._conn.execute(
                """
                UPDATE agent_tasks SET
                    status = 'COMPLETED',
                    result_json = ?,
                    worker_id = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE task_id = ?
                """,
                [json.dumps(result or {}), now_str, task_id],
            )
            self.store._conn.execute(
                """
                UPDATE worker_registry SET
                    status = 'IDLE',
                    current_task_id = NULL,
                    tasks_completed = tasks_completed + 1,
                    last_heartbeat_at = ?,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                [now_str, now_str, worker_id],
            )
            self._reconcile_dependents_on_parent_change(task_id, "COMPLETED")
            self.log_activity(
                actor=worker_id,
                agent_role=task["assigned_agent"],
                task_id=task_id,
                worker_id=worker_id,
                event_type="TASK_COMPLETED",
                summary=f"Task {task_id} completed by {worker_id}",
                detail={"result": result or {}},
            )
            completed = self.get_task(task_id)
            assert completed is not None
            return completed

    # ------------------------------------------------------------------
    # Failure Handling, Lease/Timeout/Heartbeat Reaper & Escalation
    # ------------------------------------------------------------------
    def fail_or_recover_task(
        self,
        task_id: str,
        *,
        error_message: str,
        worker_id: str | None = None,
        lease_token: str | None = None,
        explicit_class: str | None = None,
        proposed_strategies: list[str] | None = None,
        is_supervisor: bool = False,
        mark_worker_unhealthy: bool = False,
    ) -> dict[str, Any]:
        now_str = utc_now_iso()
        with self.store._lock:
            task = self.get_task(task_id)
            if not task:
                raise ValueError(f"Unknown task_id '{task_id}'")
            if not is_supervisor:
                self._assert_lease_ownership(
                    task_id, worker_id or "", lease_token or "", ("LEASED", "RUNNING")
                )

            eff_worker = worker_id or task.get("worker_id")
            if eff_worker:
                w_status = "UNHEALTHY" if mark_worker_unhealthy else "IDLE"
                self.store._conn.execute(
                    """
                    UPDATE worker_registry SET
                        status = ?,
                        current_task_id = NULL,
                        tasks_failed = tasks_failed + 1,
                        updated_at = ?
                    WHERE worker_id = ?
                    """,
                    [w_status, now_str, eff_worker],
                )

            rec = self.blocker_engine.handle_blocker(
                task=task,
                error_message=error_message,
                explicit_class=explicit_class,
                worker_id=eff_worker,
                proposed_strategies=proposed_strategies,
                is_supervisor=is_supervisor,
            )

            new_rec_attempts = int(task["recovery_attempts"]) + 1
            attempts_used = int(task["attempt_count"])
            max_retries = int(task["max_retries"])
            max_rec = int(task["max_recovery_attempts"])
            kill_active = self.store.is_kill_switch_active()

            can_requeue = (
                rec["should_retry"]
                and not kill_active
                and int(task["recovery_attempts"]) < max_rec
                and attempts_used <= max_retries
            )

            merged_payload = {**task["payload"], **(rec.get("payload_patch") or {})}
            if can_requeue:
                self.store._conn.execute(
                    """
                    UPDATE agent_tasks SET
                        status = 'QUEUED',
                        payload_json = ?,
                        worker_id = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        started_at = NULL,
                        recovery_attempts = ?,
                        last_blocker_id = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    [
                        json.dumps(merged_payload),
                        new_rec_attempts,
                        rec["blocker_id"],
                        now_str,
                        task_id,
                    ],
                )
                self.log_activity(
                    actor="supervisor" if is_supervisor else (eff_worker or "agent"),
                    agent_role=task["assigned_agent"],
                    task_id=task_id,
                    worker_id=eff_worker,
                    event_type="TASK_REQUEUED_AFTER_RECOVERY",
                    summary=f"Recovered {rec['blocker_class']} via {rec['selected_strategy']}; requeued ({new_rec_attempts}/{max_rec})",
                    detail={"blocker_id": rec["blocker_id"], "strategy": rec["selected_strategy"]},
                )
            else:
                esc_reason = rec["verification_detail"]
                self.store._conn.execute(
                    """
                    UPDATE agent_tasks SET
                        status = 'ESCALATED',
                        worker_id = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        recovery_attempts = ?,
                        last_blocker_id = ?,
                        escalation_reason = ?,
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    [new_rec_attempts, rec["blocker_id"], esc_reason, now_str, task_id],
                )
                self._reconcile_dependents_on_parent_change(task_id, "ESCALATED")
                self.log_activity(
                    actor="supervisor",
                    agent_role=task["assigned_agent"],
                    task_id=task_id,
                    worker_id=eff_worker,
                    event_type="TASK_ESCALATED",
                    summary=f"Escalated {task_id}: {esc_reason}",
                    detail={"blocker_id": rec["blocker_id"], "blocker_class": rec["blocker_class"]},
                )

            updated_task = self.get_task(task_id)
            assert updated_task is not None
            return {"task": updated_task, "blocker": rec, "requeued": can_requeue}

    def reap_stale_tasks(
        self,
        *,
        now_iso: str | None = None,
        heartbeat_grace_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        """Detect expired leases, heartbeat failures, and task timeouts; recover or escalate."""
        pol = self.get_policy()
        grace_sec = float(
            heartbeat_grace_seconds
            if heartbeat_grace_seconds is not None
            else pol["heartbeat_grace_seconds"]
        )
        now_dt = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
        events: list[dict[str, Any]] = []

        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM agent_tasks WHERE status IN ('LEASED', 'RUNNING')"
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            active_tasks = [self._row_to_task(cols, r) for r in rows]

            for task in active_tasks:
                reason_class: str | None = None
                err_msg = ""

                if task.get("lease_expires_at") and _parse_iso(task["lease_expires_at"]) <= now_dt:
                    reason_class = "WORKER_LEASE_EXPIRED"
                    err_msg = f"Lease expired at {task['lease_expires_at']}"
                elif (
                    task["status"] == "RUNNING"
                    and task.get("started_at")
                    and (_parse_iso(task["started_at"]) + timedelta(seconds=float(task["timeout_seconds"])))
                    <= now_dt
                ):
                    reason_class = "TASK_TIMEOUT"
                    err_msg = f"Task execution exceeded timeout_seconds={task['timeout_seconds']}"
                elif (
                    task.get("last_heartbeat_at")
                    and (_parse_iso(task["last_heartbeat_at"]) + timedelta(seconds=grace_sec)) <= now_dt
                ):
                    reason_class = "HEARTBEAT_LOST"
                    err_msg = f"Worker heartbeat stale past {grace_sec}s grace window"

                if reason_class:
                    res = self.fail_or_recover_task(
                        task["task_id"],
                        error_message=err_msg,
                        explicit_class=reason_class,
                        is_supervisor=True,
                        mark_worker_unhealthy=True,
                    )
                    events.append(res)

        return events

    # ------------------------------------------------------------------
    # Orchestrated Dispatch & Bounded Execution
    # ------------------------------------------------------------------
    async def dispatch_cycle(
        self,
        *,
        max_tasks: int = 4,
        custom_handlers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reconcile stale leases, dynamically scale workers, and execute ready tasks safely."""
        if self.store.is_kill_switch_active():
            scale_info = self.scale_workers(0)
            self.log_activity(
                actor="supervisor",
                agent_role="SUPERVISOR",
                event_type="DISPATCH_HALTED_KILL_SWITCH",
                summary="Supervisor halted dispatch because Global Kill Switch is active",
            )
            return {
                "halted": True,
                "reason": "KILL_SWITCH_ACTIVE",
                "reaped_count": 0,
                "dispatched_count": 0,
                "completed_count": 0,
                "recovered_or_escalated_count": 0,
                "scaling": scale_info,
                "outcomes": [],
            }

        reaped = self.reap_stale_tasks()
        pol = self.get_policy()
        scale_info = self.scale_workers()
        limit = max(1, min(int(max_tasks), pol["max_concurrency"]))

        idle_workers = [w for w in self.list_workers() if w["status"] == "IDLE"]
        leased_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for w in idle_workers:
            if len(leased_pairs) >= limit:
                break
            claimed = self.claim_next_task(w["worker_id"])
            if claimed:
                leased_pairs.append((w, claimed))

        ctx = AgentExecutionContext(
            store=self.store,
            risk_gate=self.risk_gate,
            memory=self.memory,
            learning=self.learning,
            scanner=self.scanner,
            cross_scanner=self.cross_scanner,
            custom_handlers=custom_handlers,
        )

        async def _run_one(w: dict[str, Any], leased_task: dict[str, Any]) -> dict[str, Any]:
            t_id = leased_task["task_id"]
            w_id = w["worker_id"]
            token = leased_task["lease_token"]
            try:
                running_task = self.start_task(t_id, worker_id=w_id, lease_token=token)
                timeout = float(running_task.get("timeout_seconds") or 30.0)
                output = await asyncio.wait_for(execute_agent_task(running_task, ctx), timeout=timeout)
                done = self.complete_task(t_id, worker_id=w_id, lease_token=token, result=output)
                return {"task_id": t_id, "status": done["status"], "result": output}
            except asyncio.TimeoutError:
                rec = self.fail_or_recover_task(
                    t_id,
                    worker_id=w_id,
                    lease_token=token,
                    error_message=f"Task timeout after {leased_task.get('timeout_seconds')}s",
                    explicit_class="TASK_TIMEOUT",
                    is_supervisor=True,
                )
                return {
                    "task_id": t_id,
                    "status": rec["task"]["status"],
                    "blocker": rec["blocker"]["blocker_class"],
                    "requeued": rec["requeued"],
                }
            except Exception as exc:
                rec = self.fail_or_recover_task(
                    t_id,
                    worker_id=w_id,
                    lease_token=token,
                    error_message=str(exc),
                )
                return {
                    "task_id": t_id,
                    "status": rec["task"]["status"],
                    "blocker": rec["blocker"]["blocker_class"],
                    "requeued": rec["requeued"],
                }

        outcomes = await asyncio.gather(*[_run_one(w, t) for w, t in leased_pairs]) if leased_pairs else []
        # Scale down excess idle workers after batch completes
        post_scale = self.scale_workers()
        completed_count = sum(1 for o in outcomes if o.get("status") == "COMPLETED")
        rec_count = sum(1 for o in outcomes if o.get("status") != "COMPLETED")

        return {
            "halted": False,
            "reaped_count": len(reaped),
            "dispatched_count": len(leased_pairs),
            "completed_count": completed_count,
            "recovered_or_escalated_count": rec_count,
            "scaling": post_scale,
            "outcomes": outcomes,
        }

    def get_status_snapshot(self) -> dict[str, Any]:
        pol = self.get_policy()
        kill_active = self.store.is_kill_switch_active()
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT status, COUNT(*) FROM agent_tasks GROUP BY status"
            ).fetchall()
            counts_by_status = {str(s): int(c) for s, c in rows}
        workers = self.list_workers(include_stopped=False)
        return {
            "phase": "Phase 3 — Stage 3C Multi-Agent Orchestration",
            "live_trading_enabled": False,
            "kill_switch_active": kill_active,
            "dispatch_status": "HALTED_BY_KILL_SWITCH" if kill_active else "ACTIVE_RESEARCH_ONLY",
            "policy": pol,
            "agents": [
                {
                    "role": p.role,
                    "agent_id": p.agent_id,
                    "description": p.description,
                    "permitted_actions": list(p.permitted_actions),
                    "forbidden_actions": list(p.forbidden_actions),
                    "supports_task_types": list(p.supports_task_types),
                }
                for p in AGENT_PROFILES.values()
            ],
            "active_workers": workers,
            "active_workers_count": len(workers),
            "task_counts_by_status": counts_by_status,
            "recent_blockers": self.blocker_engine.list_blockers(limit=20),
            "recent_activity": self.list_activity(limit=30),
        }
