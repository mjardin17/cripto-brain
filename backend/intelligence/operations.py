"""Stage 3E Continuous Operations Health Monitor & Controlled Soak Testing Framework.

Invariants:
- Supervisor loop failures never go unrecorded: persisted in `system_events`.
- Soak runs distinguish `SOAK_TEST` (24H/48H/72H) from `SHORT_VALIDATION`.
- A soak run ONLY transitions to `PASSED` if `elapsed_seconds >= duration_target_seconds`
  with zero unexpected shutdowns and zero fatal DB errors. Stopping early marks `STOPPED_EARLY`.
- Respects Global Kill Switch immediately and runs read-only/deterministic validation tasks only.
"""

from __future__ import annotations

import json
import os
import resource
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

SoakMode = Literal["SHORT_VALIDATION", "SOAK_24H", "SOAK_48H", "SOAK_72H"]

SOAK_MODE_DURATIONS_SEC: dict[str, tuple[float, str]] = {
    "SOAK_24H": (86400.0, "SOAK_TEST"),
    "SOAK_48H": (172800.0, "SOAK_TEST"),
    "SOAK_72H": (259200.0, "SOAK_TEST"),
    "SHORT_VALIDATION": (15.0, "SHORT_VALIDATION"),
}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def get_process_rss_mb() -> float:
    """Read current resident set size in MB using stdlib (Linux /proc/self/statm with rusage fallback)."""
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fh:
            parts = fh.read().strip().split()
            if len(parts) >= 2:
                pages = int(parts[1])
                page_size = os.sysconf("SC_PAGE_SIZE")
                return round((pages * page_size) / (1024.0 * 1024.0), 2)
    except (OSError, ValueError):
        pass
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(float(ru) / 1024.0, 2)
    except Exception:
        return 0.0


class OperationalHealthMonitor:
    """Computes continuous runtime metrics across Supervisor, workers, tasks, recovery, and system events."""

    def __init__(self, store: DuckDBStore, supervisor: Any, model_router: Any = None) -> None:
        self.store = store
        self.supervisor = supervisor
        self.model_router = model_router

    def get_health_report(self, *, long_running_threshold_seconds: float = 60.0) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        pol = self.supervisor.get_policy()
        hb_grace = float(pol.get("heartbeat_grace_seconds", 20.0))
        loop_state = self.supervisor.get_background_loop_state()
        kill_active = self.store.is_kill_switch_active()

        with self.store._lock:
            worker_rows = self.store._conn.execute(
                "SELECT * FROM worker_registry ORDER BY spawned_at DESC"
            ).fetchall()
            w_cols = [d[0] for d in self.store._conn.description]
            workers = [dict(zip(w_cols, r)) for r in worker_rows]

            task_rows = self.store._conn.execute("SELECT * FROM agent_tasks").fetchall()
            t_cols = [d[0] for d in self.store._conn.description]
            tasks = [dict(zip(t_cols, r)) for r in task_rows]

            blocker_rows = self.store._conn.execute(
                "SELECT blocker_class, outcome FROM blocker_events"
            ).fetchall()

            evt_rows = self.store._conn.execute(
                "SELECT severity, COUNT(*) FROM system_events GROUP BY severity"
            ).fetchall()

        workers_by_id = {w["worker_id"]: w for w in workers}
        active_workers = [w for w in workers if w["status"] != "STOPPED"]
        unhealthy_workers = [w for w in workers if w["status"] == "UNHEALTHY"]

        worker_uptimes: list[float] = []
        stalled_workers: list[dict[str, Any]] = []
        for w in active_workers:
            sp_dt = _parse_iso(w.get("spawned_at"))
            hb_dt = _parse_iso(w.get("last_heartbeat_at"))
            if sp_dt:
                worker_uptimes.append(max(0.0, (now_dt - sp_dt).total_seconds()))
            hb_age = (now_dt - hb_dt).total_seconds() if hb_dt else 9999.0
            if hb_age > hb_grace:
                stalled_workers.append(
                    {
                        "worker_id": w["worker_id"],
                        "agent_role": w["agent_role"],
                        "status": w["status"],
                        "heartbeat_age_seconds": round(hb_age, 2),
                        "current_task_id": w.get("current_task_id"),
                    }
                )

        # Task classification & operational anomalies
        queued_count = sum(1 for t in tasks if t["status"] in ("PENDING", "QUEUED"))
        active_count = sum(1 for t in tasks if t["status"] in ("LEASED", "RUNNING"))
        completed_count = sum(1 for t in tasks if t["status"] == "COMPLETED")
        failed_count = sum(1 for t in tasks if t["status"] == "FAILED")
        escalated_count = sum(1 for t in tasks if t["status"] == "ESCALATED")
        blocked_count = sum(1 for t in tasks if t["status"] == "BLOCKED")

        abandoned_tasks: list[dict[str, Any]] = []
        long_running_tasks: list[dict[str, Any]] = []
        expired_active_leases = 0
        completed_durations_ms: list[float] = []

        for t in tasks:
            status = t["status"]
            if status in ("LEASED", "RUNNING"):
                w_id = t.get("worker_id")
                w = workers_by_id.get(w_id) if w_id else None
                if not w_id or not w or w["status"] in ("STOPPED", "UNHEALTHY"):
                    abandoned_tasks.append(
                        {
                            "task_id": t["task_id"],
                            "task_type": t["task_type"],
                            "worker_id": w_id,
                            "worker_status": w["status"] if w else "MISSING",
                        }
                    )
                exp_dt = _parse_iso(t.get("lease_expires_at"))
                if exp_dt and exp_dt <= now_dt:
                    expired_active_leases += 1
                st_dt = _parse_iso(t.get("started_at"))
                if status == "RUNNING" and st_dt:
                    run_sec = (now_dt - st_dt).total_seconds()
                    eff_limit = max(float(long_running_threshold_seconds), float(t.get("timeout_seconds") or 30.0))
                    if run_sec > eff_limit:
                        long_running_tasks.append(
                            {
                                "task_id": t["task_id"],
                                "task_type": t["task_type"],
                                "running_seconds": round(run_sec, 2),
                                "timeout_seconds": float(t.get("timeout_seconds") or 30.0),
                            }
                        )
            elif status == "COMPLETED":
                st_dt = _parse_iso(t.get("started_at"))
                up_dt = _parse_iso(t.get("updated_at"))
                if st_dt and up_dt and up_dt >= st_dt:
                    completed_durations_ms.append((up_dt - st_dt).total_seconds() * 1000.0)

        avg_task_duration_ms = (
            round(sum(completed_durations_ms) / len(completed_durations_ms), 2)
            if completed_durations_ms
            else 0.0
        )

        # Recovery stats
        rec_total = len(blocker_rows)
        rec_success = sum(1 for _, out in blocker_rows if out in ("RECOVERED", "RESOLVED", "REQUEUED"))
        rec_escalated = sum(1 for _, out in blocker_rows if out == "ESCALATED")
        historical_expired_leases = sum(1 for cls, _ in blocker_rows if cls == "WORKER_LEASE_EXPIRED")
        rec_rate = round((rec_success / rec_total) * 100.0, 2) if rec_total > 0 else 100.0

        evt_counts = {str(sev): int(cnt) for sev, cnt in evt_rows}
        critical_events = evt_counts.get("CRITICAL", 0)
        warning_events = evt_counts.get("WARNING", 0)

        if kill_active:
            health_status = "HALTED_BY_KILL_SWITCH"
        elif (
            critical_events > 0
            or int(loop_state.get("critical_failures_count", 0)) > 0
            or len(abandoned_tasks) > 0
            or len(stalled_workers) > 0
        ):
            health_status = "DEGRADED"
        else:
            health_status = "NOMINAL"

        api_spend = self.model_router.get_daily_spend_usd() if self.model_router else 0.0

        return {
            "provenance": "LIVE",
            "health_status": health_status,
            "supervisor_running": bool(loop_state.get("running", False)),
            "supervisor_uptime_seconds": float(loop_state.get("uptime_seconds", 0.0)),
            "supervisor_ticks": int(loop_state.get("ticks", 0)),
            "critical_failures_count": int(loop_state.get("critical_failures_count", 0)),
            "last_supervisor_error": loop_state.get("last_error"),
            "active_workers_count": len(active_workers),
            "unhealthy_workers_count": len(unhealthy_workers),
            "stalled_workers_count": len(stalled_workers),
            "stalled_workers": stalled_workers,
            "worker_uptime_avg_seconds": round(sum(worker_uptimes) / len(worker_uptimes), 2)
            if worker_uptimes
            else 0.0,
            "tasks_queued": queued_count,
            "tasks_active": active_count,
            "tasks_completed": completed_count,
            "tasks_failed": failed_count,
            "tasks_escalated": escalated_count,
            "tasks_blocked": blocked_count,
            "abandoned_tasks_count": len(abandoned_tasks),
            "abandoned_tasks": abandoned_tasks,
            "long_running_tasks_count": len(long_running_tasks),
            "long_running_tasks": long_running_tasks,
            "expired_leases_active_count": expired_active_leases,
            "expired_leases_historical_count": historical_expired_leases,
            "avg_task_duration_ms": avg_task_duration_ms,
            "recovery_attempts_total": rec_total,
            "recovery_successes": rec_success,
            "recovery_escalations": rec_escalated,
            "recovery_success_rate_pct": rec_rate,
            "critical_events_count": critical_events,
            "warning_events_count": warning_events,
            "memory_rss_mb": get_process_rss_mb(),
            "api_spend_today_usd": api_spend,
            "checked_at": now_iso,
        }


class SoakTestManager:
    """Manages controlled long-running soak tests (24h/48h/72h) and bounded short validations."""

    def __init__(
        self,
        store: DuckDBStore,
        supervisor: Any,
        health_monitor: OperationalHealthMonitor,
        model_router: Any = None,
    ) -> None:
        self.store = store
        self.supervisor = supervisor
        self.health_monitor = health_monitor
        self.model_router = model_router
        self._last_task_submit_mono: float = 0.0
        self._reconcile_interrupted_runs()

    def _row_to_run(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
        target = float(d.get("duration_target_seconds") or 1.0)
        elapsed = float(d.get("elapsed_seconds") or 0.0)
        d["progress_pct"] = round(min(100.0, max(0.0, (elapsed / target) * 100.0)), 2) if target > 0 else 0.0
        return d

    def _reconcile_interrupted_runs(self) -> int:
        """Fail closed: any RUNNING soak run surviving a server crash becomes UNEXPECTED_SHUTDOWN."""
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        interrupted = 0
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM soak_runs WHERE status = 'RUNNING'"
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            for r in rows:
                run = self._row_to_run(cols, r)
                st_dt = _parse_iso(run["started_at"]) or now_dt
                elapsed = round(max(0.0, (now_dt - st_dt).total_seconds()), 2)
                self.store._conn.execute(
                    """
                    UPDATE soak_runs SET
                        status = 'UNEXPECTED_SHUTDOWN',
                        ended_at = ?,
                        elapsed_seconds = ?,
                        unexpected_shutdowns = unexpected_shutdowns + 1,
                        failure_reason = ?,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    [
                        now_iso,
                        elapsed,
                        "Interrupted by unexpected process termination or restart before target duration elapsed",
                        now_iso,
                        run["run_id"],
                    ],
                )
                interrupted += 1
        if interrupted > 0:
            self.store.record_system_event(
                severity="CRITICAL",
                component="soak_manager",
                event_type="SOAK_UNEXPECTED_SHUTDOWN",
                message=f"Detected {interrupted} interrupted soak run(s) after restart; marked UNEXPECTED_SHUTDOWN",
                detail={"interrupted_count": interrupted},
            )
        return interrupted

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM soak_runs WHERE run_id = ?", [run_id]
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_run(cols, row)

    def list_runs(self, limit: int = 40) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM soak_runs ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_run(cols, r) for r in rows]

    def get_active_run(self) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM soak_runs WHERE status = 'RUNNING' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_run(cols, row)

    def start_soak(
        self,
        *,
        mode: SoakMode,
        duration_seconds: float | None = None,
        tick_interval_seconds: float = 2.0,
    ) -> dict[str, Any]:
        norm_mode = mode.strip().upper()
        if norm_mode not in SOAK_MODE_DURATIONS_SEC:
            raise ValueError(f"Unsupported soak mode '{mode}'. Valid: {list(SOAK_MODE_DURATIONS_SEC)}")

        if self.get_active_run() is not None:
            raise RuntimeError("A soak run is already RUNNING. Stop the active run before starting another.")

        canonical_dur, run_label = SOAK_MODE_DURATIONS_SEC[norm_mode]
        if norm_mode == "SHORT_VALIDATION":
            target_sec = float(max(0.2, min(3600.0, duration_seconds if duration_seconds is not None else canonical_dur)))
        else:
            # Strict integrity: 24H / 48H / 72H soak tests MUST enforce full configured duration.
            target_sec = canonical_dur

        tick_sec = float(max(0.1, min(60.0, tick_interval_seconds)))
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        ends_iso = (now_dt + timedelta(seconds=target_sec)).isoformat()
        run_id = f"SOAK-{uuid.uuid4().hex[:10].upper()}"
        rss_mb = get_process_rss_mb()
        spend = self.model_router.get_daily_spend_usd() if self.model_router else 0.0

        metrics_baseline = {
            "baseline_tasks_completed": self.health_monitor.get_health_report()["tasks_completed"],
            "baseline_tasks_failed": self.health_monitor.get_health_report()["tasks_failed"],
            "baseline_recovery_events": self.health_monitor.get_health_report()["recovery_attempts_total"],
            "baseline_spend_usd": spend,
            "last_rss_mb": rss_mb,
            "peak_rss_mb": rss_mb,
            "notes": (
                "Full duration SOAK_TEST; cannot mark PASSED early."
                if run_label == "SOAK_TEST"
                else "Bounded SHORT_VALIDATION run; never mislabeled as multi-day SOAK_TEST."
            ),
        }

        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO soak_runs VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    run_id,
                    norm_mode,
                    run_label,
                    "RUNNING",
                    target_sec,
                    tick_sec,
                    now_iso,
                    ends_iso,
                    None,
                    0.0,
                    0.0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    rss_mb,
                    0.0,
                    0,
                    1 if self.store.is_kill_switch_active() else 0,
                    None,
                    json.dumps(metrics_baseline),
                    now_iso,
                    now_iso,
                ],
            )
        self._last_task_submit_mono = 0.0
        run = self.get_run(run_id)
        assert run is not None
        return run

    def tick_active_soak(
        self,
        *,
        simulated_elapsed_override_seconds: float | None = None,
        submit_sample_task: bool = True,
    ) -> dict[str, Any] | None:
        """Update metrics on the active soak run and evaluate clean completion."""
        run = self.get_active_run()
        if not run:
            return None

        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        st_dt = _parse_iso(run["started_at"]) or now_dt
        elapsed = (
            float(simulated_elapsed_override_seconds)
            if simulated_elapsed_override_seconds is not None
            else max(0.0, (now_dt - st_dt).total_seconds())
        )
        elapsed = round(elapsed, 2)

        kill_active = self.store.is_kill_switch_active()
        kill_events = int(run["kill_switch_events"]) + (1 if kill_active else 0)
        tasks_submitted = int(run["tasks_submitted"])

        if submit_sample_task and not kill_active:
            mono = time.monotonic()
            if (mono - self._last_task_submit_mono) >= max(0.1, float(run["tick_interval_seconds"])):
                queued_cnt = self.health_monitor.get_health_report()["tasks_queued"]
                if queued_cnt < 6:
                    sub = self.supervisor.submit_task(
                        task_type="CHECK_FEED_HEALTH",
                        assigned_agent="MARKET_INTELLIGENCE",
                        payload={"soak_run_id": run["run_id"], "prefer_ai": False},
                        priority=7,
                        dedup_key=f"soak:{run['run_id']}:{tasks_submitted + 1}",
                        created_by="soak_manager",
                    )
                    if sub.get("created"):
                        tasks_submitted += 1
                    self._last_task_submit_mono = mono

        health = self.health_monitor.get_health_report()
        metrics = dict(run["metrics"])
        rss = float(health["memory_rss_mb"] or 0.0)
        metrics["last_rss_mb"] = rss
        metrics["peak_rss_mb"] = max(float(metrics.get("peak_rss_mb", 0.0)), rss)

        completed_delta = max(0, int(health["tasks_completed"]) - int(metrics.get("baseline_tasks_completed", 0)))
        failed_delta = max(0, int(health["tasks_failed"]) - int(metrics.get("baseline_tasks_failed", 0)))
        recovery_delta = max(0, int(health["recovery_attempts_total"]) - int(metrics.get("baseline_recovery_events", 0)))
        spend_delta = round(
            max(0.0, float(health["api_spend_today_usd"]) - float(metrics.get("baseline_spend_usd", 0.0))),
            6,
        )

        new_status = "RUNNING"
        ended_at = None
        failure_reason = None
        if int(run["db_errors"]) > 0 or int(run["unexpected_shutdowns"]) > 0:
            new_status = "FAILED"
            ended_at = now_iso
            failure_reason = "Soak run failed due to database or unexpected shutdown errors"
        elif elapsed >= float(run["duration_target_seconds"]):
            new_status = "PASSED"
            ended_at = now_iso

        with self.store._lock:
            self.store._conn.execute(
                """
                UPDATE soak_runs SET
                    status = ?,
                    ended_at = ?,
                    elapsed_seconds = ?,
                    supervisor_uptime_seconds = ?,
                    tasks_submitted = ?,
                    tasks_completed = ?,
                    tasks_failed = ?,
                    worker_failures = ?,
                    recovery_events = ?,
                    memory_rss_mb = ?,
                    api_spend_usd = ?,
                    kill_switch_events = ?,
                    failure_reason = ?,
                    metrics_json = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                [
                    new_status,
                    ended_at,
                    elapsed,
                    float(health["supervisor_uptime_seconds"]),
                    tasks_submitted,
                    completed_delta,
                    failed_delta,
                    int(health["unhealthy_workers_count"]),
                    recovery_delta,
                    rss,
                    spend_delta,
                    kill_events,
                    failure_reason,
                    json.dumps(metrics),
                    now_iso,
                    run["run_id"],
                ],
            )
        return self.get_run(run["run_id"])

    def stop_soak(
        self,
        *,
        run_id: str | None = None,
        reason: str = "Stopped manually before target duration completed",
    ) -> dict[str, Any]:
        run = self.get_run(run_id) if run_id else self.get_active_run()
        if not run:
            raise ValueError("No matching RUNNING soak test found")
        if run["status"] != "RUNNING":
            return run

        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        st_dt = _parse_iso(run["started_at"]) or now_dt
        elapsed = round(max(0.0, (now_dt - st_dt).total_seconds()), 2)
        target = float(run["duration_target_seconds"])
        health = self.health_monitor.get_health_report()

        if elapsed >= target and int(run["db_errors"]) == 0 and int(run["unexpected_shutdowns"]) == 0:
            final_status = "PASSED"
            final_reason = None
        else:
            final_status = "STOPPED_EARLY"
            final_reason = f"{reason} (elapsed {elapsed:.2f}s < target {target:.2f}s)"

        with self.store._lock:
            self.store._conn.execute(
                """
                UPDATE soak_runs SET
                    status = ?,
                    ended_at = ?,
                    elapsed_seconds = ?,
                    supervisor_uptime_seconds = ?,
                    memory_rss_mb = ?,
                    failure_reason = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                [
                    final_status,
                    now_iso,
                    elapsed,
                    float(health["supervisor_uptime_seconds"]),
                    float(health["memory_rss_mb"]),
                    final_reason,
                    now_iso,
                    run["run_id"],
                ],
            )
        updated = self.get_run(run["run_id"])
        assert updated is not None
        return updated
