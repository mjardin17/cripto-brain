"""Stage 3E Operational & Intelligence Validation Laboratories.

Implements:
- Part 4: Multi-Agent Concurrency Stress Lab (`ConcurrencyStressLab`)
- Part 6: Research Quality, Evidence Provenance & Prompt Injection Firewall (`ResearchQualityValidator`)
- Part 7: Paper Execution Stress Laboratory (`PaperStressLab` across all 12 required scenarios)
- Part 8: Adversarial Research Review (`AdversarialReviewEngine`: Proposer -> Challenger -> Arbiter)
- Part 10: Evidence-backed integration with Stage 3B LearningEngine without self-promotion or policy mutation
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.arbitrage.calculator import NormalizedOrderBook, OrderBookLevel
from backend.arbitrage.matcher import assess_contract_equivalence
from backend.execution.paper_simulator import PaperExecutionEngine, PaperExecutionRequest
from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import StructuredMemoryManager
from backend.intelligence.supervisor import SupervisorOrchestrator
from backend.risk_gate.engine import DeterministicRiskGate
from backend.risk_gate.policy import RiskPolicy
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import KalshiBinaryMarket, KalshiBookSnapshot
from backend.venues.polymarket import PolymarketBinaryMarket, PolymarketBookSnapshot


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ======================================================================
# PART 4: Multi-Agent Concurrency Stress Lab
# ======================================================================
class ConcurrencyStressLab:
    """Runs isolated deterministic multi-agent concurrency & recovery stress tests."""

    def __init__(
        self,
        store: DuckDBStore,
        learning: LearningEngine,
    ) -> None:
        self.store = store
        self.learning = learning

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM concurrency_stress_runs ORDER BY ended_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["config"] = json.loads(d.pop("config_json", "{}"))
                d["summary"] = json.loads(d.pop("summary_json", "{}"))
                out.append(d)
            return out

    async def run_stress_suite(
        self,
        *,
        task_count: int = 12,
        worker_count: int = 4,
    ) -> dict[str, Any]:
        started_iso = utc_now_iso()
        t_start = time.monotonic()
        bounded_workers = max(2, min(8, int(worker_count)))
        bounded_tasks = max(4, min(32, int(task_count)))
        stress_id = f"CSTRESS-{uuid.uuid4().hex[:10].upper()}"

        # Run inside an isolated temporary DuckDB so live portfolio/tasks are unaffected
        with tempfile.TemporaryDirectory(prefix="certainty_stress_") as tmpdir:
            db_file = f"{tmpdir}/stress.duckdb"
            iso_store = DuckDBStore(db_path=db_file)
            iso_mem = StructuredMemoryManager(store=iso_store)
            iso_learn = LearningEngine(store=iso_store, memory_manager=iso_mem)
            iso_gate = DeterministicRiskGate(store=iso_store, policy=RiskPolicy())
            iso_sup = SupervisorOrchestrator(
                store=iso_store,
                risk_gate=iso_gate,
                memory=iso_mem,
                learning=iso_learn,
            )
            iso_sup.set_max_concurrency(bounded_workers)

            total_claims_attempted = 0
            successful_claims = 0
            rejected_duplicates = 0
            ownership_violations = 0
            recovery_successes = 0
            db_contention_retries = 0

            try:
                # 1. Duplicate task submission protection
                dup_key = f"stress:dup:{stress_id}"
                sub1 = iso_sup.submit_task(
                    task_type="CHECK_FEED_HEALTH",
                    assigned_agent="MARKET_INTELLIGENCE",
                    payload={"synthetic": True, "seq": 0},
                    dedup_key=dup_key,
                    created_by="concurrency_stress_lab",
                )
                sub2 = iso_sup.submit_task(
                    task_type="CHECK_FEED_HEALTH",
                    assigned_agent="MARKET_INTELLIGENCE",
                    payload={"synthetic": True, "seq": 0},
                    dedup_key=dup_key,
                    created_by="concurrency_stress_lab",
                )
                if not sub2["created"] and sub2["duplicate_prevented"]:
                    rejected_duplicates += 1

                # 2. Submit remaining synthetic tasks + dependency chain
                submitted_ids = [sub1["task"]["task_id"]]
                for idx in range(1, bounded_tasks - 2):
                    res = iso_sup.submit_task(
                        task_type="AUDIT_RISK_STATE" if idx % 2 == 0 else "CHECK_FEED_HEALTH",
                        assigned_agent="RISK_INTELLIGENCE" if idx % 2 == 0 else "MARKET_INTELLIGENCE",
                        payload={"synthetic": True, "seq": idx},
                        dedup_key=f"stress:task:{stress_id}:{idx}",
                        priority=1 + (idx % 5),
                        created_by="concurrency_stress_lab",
                    )
                    if res["created"]:
                        submitted_ids.append(res["task"]["task_id"])

                # Dependency chain parent -> child
                dep_parent = iso_sup.submit_task(
                    task_type="CALIBRATE_PROBABILITY",
                    assigned_agent="PROBABILITY_RESEARCH",
                    payload={"synthetic": True, "role": "parent"},
                    dedup_key=f"stress:parent:{stress_id}",
                    created_by="concurrency_stress_lab",
                )["task"]
                dep_child = iso_sup.submit_task(
                    task_type="CONSOLIDATE_LEARNING",
                    assigned_agent="LEARNING",
                    payload={"synthetic": True, "role": "child"},
                    depends_on=[dep_parent["task_id"]],
                    dedup_key=f"stress:child:{stress_id}",
                    created_by="concurrency_stress_lab",
                )["task"]
                assert dep_child["status"] == "PENDING"

                # 3. Competing concurrent task claims & ownership / fencing violation checks
                w_a = iso_sup.spawn_worker("MARKET_INTELLIGENCE")
                w_b = iso_sup.spawn_worker("MARKET_INTELLIGENCE")
                total_claims_attempted += 2

                claimed_pair = await asyncio.gather(
                    asyncio.to_thread(iso_sup.claim_next_task, w_a["worker_id"]),
                    asyncio.to_thread(iso_sup.claim_next_task, w_b["worker_id"]),
                )
                claimed_ids: list[str] = []
                for w_obj, c_task in zip((w_a, w_b), claimed_pair):
                    if c_task is not None:
                        successful_claims += 1
                        claimed_ids.append(c_task["task_id"])
                        # Attempt unauthorized access by bogus worker/token (fencing check)
                        try:
                            iso_sup.start_task(
                                c_task["task_id"],
                                worker_id=w_obj["worker_id"],
                                lease_token="LEASE-FORGED-TOKEN",
                            )
                        except PermissionError:
                            ownership_violations += 1
                        iso_sup.start_task(
                            c_task["task_id"],
                            worker_id=w_obj["worker_id"],
                            lease_token=c_task["lease_token"],
                        )
                        iso_sup.complete_task(
                            c_task["task_id"],
                            worker_id=w_obj["worker_id"],
                            lease_token=c_task["lease_token"],
                            result={"synthetic_ok": True},
                        )

                # Ensure two competing workers never claimed the same task
                double_claim_detected = len(claimed_ids) != len(set(claimed_ids))
                if double_claim_detected:
                    ownership_violations += 1

                # Stop temporary workers so dynamic scaler starts clean
                iso_sup.stop_worker(w_a["worker_id"], reason="Stress phase 1 complete")
                iso_sup.stop_worker(w_b["worker_id"], reason="Stress phase 1 complete")

                # 4. Simulated worker crash + lease expiration & heartbeat loss recovery
                w_crash = iso_sup.spawn_worker("RISK_INTELLIGENCE")
                total_claims_attempted += 1
                crashed_task = iso_sup.claim_next_task(w_crash["worker_id"], lease_duration_seconds=1.0)
                if crashed_task:
                    successful_claims += 1
                    future_iso = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
                    reaped = iso_sup.reap_stale_tasks(now_iso=future_iso)
                    if reaped:
                        recovery_successes += len(reaped)
                # Also verify explicit heartbeat loss detection
                w_hb = iso_sup.spawn_worker("RISK_INTELLIGENCE")
                total_claims_attempted += 1
                hb_task = iso_sup.claim_next_task(w_hb["worker_id"], lease_duration_seconds=300.0)
                if hb_task:
                    successful_claims += 1
                    hb_future = (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat()
                    hb_reaped = iso_sup.reap_stale_tasks(now_iso=hb_future, heartbeat_grace_seconds=5.0)
                    if hb_reaped:
                        recovery_successes += len(hb_reaped)

                # 5. Concurrent dispatch & concurrent blocker recovery (custom transient failure handler)
                flaky_attempts: dict[str, int] = {}

                async def _flaky_handler(task: dict[str, Any], _ctx: Any) -> dict[str, Any]:
                    tid = task["task_id"]
                    flaky_attempts[tid] = flaky_attempts.get(tid, 0) + 1
                    if flaky_attempts[tid] == 1:
                        raise RuntimeError("HTTP 429 transient rate limit during concurrency stress")
                    return {"recovered_after_retry": True, "attempts": flaky_attempts[tid]}

                iso_sup.submit_task(
                    task_type="ARBITRAGE_ANALYSIS",
                    assigned_agent="ARBITRAGE_RESEARCH",
                    payload={"synthetic": True, "flaky": True},
                    dedup_key=f"stress:flaky:{stress_id}",
                    created_by="concurrency_stress_lab",
                )

                for _ in range(10):
                    queued_rem = len(iso_sup.list_tasks(status="QUEUED")) + len(iso_sup.list_tasks(status="PENDING"))
                    if queued_rem == 0:
                        break
                    cyc = await iso_sup.dispatch_cycle(
                        max_tasks=bounded_workers,
                        custom_handlers={"ARBITRAGE_ANALYSIS": _flaky_handler},
                    )
                    total_claims_attempted += int(cyc.get("dispatched_count", 0))
                    successful_claims += int(cyc.get("dispatched_count", 0))
                    recovery_successes += int(cyc.get("recovered_or_escalated_count", 0))

                # 6. Simulated DB contention concurrent event writes
                def _contend_write_sync(i: int) -> int:
                    retries = 0
                    for attempt in range(3):
                        try:
                            iso_store.record_system_event(
                                severity="INFO",
                                component="concurrency_stress_lab",
                                event_type="CONTENTION_PROBE",
                                message=f"Contention write #{i}",
                                detail={"attempt": attempt},
                            )
                            return retries
                        except Exception:
                            retries += 1
                            time.sleep(0.01)
                    return retries

                retry_counts = await asyncio.gather(*[asyncio.to_thread(_contend_write_sync, i) for i in range(8)])
                db_contention_retries = sum(retry_counts)

                # 7. Simulated restart recovery check on persistent file DuckDB
                w_restart = iso_sup.spawn_worker("MARKET_INTELLIGENCE")
                iso_sup.submit_task(
                    task_type="CHECK_FEED_HEALTH",
                    assigned_agent="MARKET_INTELLIGENCE",
                    payload={"synthetic": True, "restart_case": True},
                    dedup_key=f"stress:restart:{stress_id}",
                    created_by="concurrency_stress_lab",
                )
                total_claims_attempted += 1
                stranded = iso_sup.claim_next_task(w_restart["worker_id"], lease_duration_seconds=1.0)
                if stranded:
                    successful_claims += 1
                iso_store.close()

                # Reopen DB without background loop shutdown (simulates process restart)
                iso_store = DuckDBStore(db_path=db_file)
                iso_mem = StructuredMemoryManager(store=iso_store)
                iso_learn = LearningEngine(store=iso_store, memory_manager=iso_mem)
                iso_gate = DeterministicRiskGate(store=iso_store, policy=RiskPolicy())
                iso_sup = SupervisorOrchestrator(
                    store=iso_store,
                    risk_gate=iso_gate,
                    memory=iso_mem,
                    learning=iso_learn,
                )
                post_restart_reap = iso_sup.reap_stale_tasks(
                    now_iso=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
                )
                recovery_successes += len(post_restart_reap)

                # 8. Kill Switch interruption mid-flight
                iso_sup.submit_task(
                    task_type="CHECK_FEED_HEALTH",
                    assigned_agent="MARKET_INTELLIGENCE",
                    payload={"synthetic": True, "kill_switch_probe": True},
                    dedup_key=f"stress:ks:{stress_id}",
                    created_by="concurrency_stress_lab",
                )
                iso_store.set_kill_switch(True, "Concurrency stress Kill Switch probe", actor="stress_lab")
                ks_dispatch = await iso_sup.dispatch_cycle(max_tasks=bounded_workers)
                kill_switch_halted = bool(ks_dispatch.get("halted") is True)
                iso_store.set_kill_switch(False, "Concurrency stress Kill Switch cleared", actor="stress_lab")
                await iso_sup.dispatch_cycle(max_tasks=bounded_workers)

                # 9. Measure latency, throughput, deadlock & starvation
                all_tasks = iso_sup.list_tasks(limit=200)
                latencies_ms: list[float] = []
                completed_total = 0
                starved = 0
                deadlocks = 0
                status_map = {t["task_id"]: t["status"] for t in all_tasks}
                for t in all_tasks:
                    if t["status"] == "COMPLETED":
                        completed_total += 1
                    if t["status"] in ("PENDING", "QUEUED"):
                        starved += 1
                    # Detect cycle or orphan deadlocks
                    for d_id in t.get("depends_on") or []:
                        if d_id not in status_map or t["task_id"] in (iso_sup.get_task(d_id) or {}).get("depends_on", []):
                            deadlocks += 1
                    cr = _parse_iso(t.get("created_at"))
                    st = _parse_iso(t.get("started_at"))
                    if cr and st and st >= cr:
                        latencies_ms.append((st - cr).total_seconds() * 1000.0)

                elapsed_sec = max(0.01, time.monotonic() - t_start)
                avg_lat_ms = round(sum(latencies_ms) / len(latencies_ms), 2) if latencies_ms else 0.0
                throughput = round(completed_total / elapsed_sec, 2)
                passed = (
                    not double_claim_detected
                    and rejected_duplicates >= 1
                    and ownership_violations >= 1
                    and recovery_successes >= 3
                    and deadlocks == 0
                    and starved == 0
                    and kill_switch_halted
                )
                final_status = "PASSED" if passed else "FAILED"
                summary_obj = {
                    "double_claim_detected": double_claim_detected,
                    "completed_synthetic_tasks": completed_total,
                    "restart_recovered_count": len(post_restart_reap),
                    "dependency_child_completed": (iso_sup.get_task(dep_child["task_id"]) or {}).get("status") == "COMPLETED",
                    "flaky_task_recovered": max(flaky_attempts.values(), default=0) >= 2,
                    "elapsed_seconds": round(elapsed_sec, 3),
                }
            finally:
                try:
                    iso_store.close()
                except Exception:
                    pass

        ended_iso = utc_now_iso()
        cfg = {"task_count": bounded_tasks, "worker_count": bounded_workers, "isolated": True}
        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO concurrency_stress_runs VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    stress_id,
                    final_status,
                    json.dumps(cfg),
                    total_claims_attempted,
                    successful_claims,
                    rejected_duplicates,
                    ownership_violations,
                    recovery_successes,
                    deadlocks,
                    starved,
                    avg_lat_ms,
                    throughput,
                    kill_switch_halted,
                    db_contention_retries,
                    json.dumps(summary_obj),
                    started_iso,
                    ended_iso,
                ],
            )

        if not passed:
            self.store.record_system_event(
                severity="CRITICAL",
                component="concurrency_stress_lab",
                event_type="CONCURRENCY_STRESS_FAILURE",
                message=f"Concurrency stress run {stress_id} detected operational failures",
                detail=summary_obj,
            )

        return {
            "stress_id": stress_id,
            "status": final_status,
            "config": cfg,
            "total_claims_attempted": total_claims_attempted,
            "successful_claims": successful_claims,
            "rejected_duplicates": rejected_duplicates,
            "ownership_violations": ownership_violations,
            "recovery_successes": recovery_successes,
            "deadlocks_detected": deadlocks,
            "starved_tasks": starved,
            "avg_queue_latency_ms": avg_lat_ms,
            "throughput_tasks_per_sec": throughput,
            "kill_switch_halted": kill_switch_halted,
            "db_contention_retries": db_contention_retries,
            "summary": summary_obj,
            "started_at": started_iso,
            "ended_at": ended_iso,
        }


# ======================================================================
# PART 6: Research Quality, Evidence Provenance & Prompt Injection Firewall
# ======================================================================
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    ("INSTRUCTION_OVERRIDE", r"\bignore\s+(all\s+)?(previous|prior|system)\s+(instructions?|prompts?|rules?)\b"),
    ("PROMPT_EXFILTRATION", r"\b(reveal|print|dump|output|show)\s+(the\s+)?(system\s+prompt|api[_\s]?key|gemini_api_key|secrets?|env)\b"),
    ("RISK_GATE_BYPASS", r"\b(bypass|disable|override|turn\s+off)\s+(the\s+)?(risk[_\s]?gate|kill[_\s]?switch|safety|limits?)\b"),
    ("LIVE_TRADING_FORCE", r"\b(enable|set)\s+live_trading_enabled\s*(=|to)?\s*true\b"),
    ("ROLE_HIJACK", r"\byou\s+are\s+now\s+(an?\s+)?(unrestricted|admin|root|trader)\b"),
    ("SCRIPT_OR_SHELL_INJECTION", r"(<\s*script\b|drop\s+table\b|;\s*rm\s+-rf|\$\([^)]+\))"),
]


class ResearchQualityValidator:
    """Validates data freshness, source provenance, conflicts, contract parity, and prompt injections."""

    DEFAULT_MAX_DATA_AGE_MS = 15_000

    def __init__(self, store: DuckDBStore, learning: LearningEngine) -> None:
        self.store = store
        self.learning = learning

    @staticmethod
    def detect_prompt_injection(*texts: str | None) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for idx, raw in enumerate(texts):
            if not raw:
                continue
            s = str(raw)
            for label, pattern in _INJECTION_PATTERNS:
                m = re.search(pattern, s, flags=re.IGNORECASE)
                if m:
                    findings.append(
                        {
                            "type": label,
                            "field_index": str(idx),
                            "matched_snippet": m.group(0)[:120],
                        }
                    )
        return findings

    def list_validations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM research_validations ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["payload"] = json.loads(d.pop("payload_json", "{}"))
                d["findings"] = json.loads(d.pop("findings_json", "[]"))
                out.append(d)
            return out

    def validate_submission(
        self,
        *,
        agent_id: str,
        source_identifier: str,
        source_timestamp: str | None,
        retrieval_timestamp: str | None = None,
        evidence_summary: str = "",
        market_id: str = "",
        market_question: str = "",
        raw_external_text: str = "",
        confidence: float = 0.70,
        conflicting_claims: list[dict[str, Any]] | None = None,
        cross_venue_check: dict[str, Any] | None = None,
        max_age_ms: int = DEFAULT_MAX_DATA_AGE_MS,
    ) -> dict[str, Any]:
        ret_dt = _parse_iso(retrieval_timestamp) or datetime.now(timezone.utc)
        ret_iso = ret_dt.isoformat()
        src_dt = _parse_iso(source_timestamp)
        findings: list[str] = []

        # 1. Prompt injection & untrusted external payload check (including nested claims & metadata)
        claims_text = json.dumps(conflicting_claims, ensure_ascii=False) if conflicting_claims else ""
        cross_text = json.dumps(cross_venue_check, ensure_ascii=False) if cross_venue_check else ""
        inj_hits = self.detect_prompt_injection(
            raw_external_text,
            evidence_summary,
            market_question,
            source_identifier,
            agent_id,
            claims_text,
            cross_text,
        )
        injection_detected = len(inj_hits) > 0
        if injection_detected:
            findings.append(
                f"Prompt/command injection blocked from untrusted external payload ({inj_hits[0]['type']})"
            )
            self.store.record_system_event(
                severity="CRITICAL",
                component="research_firewall",
                event_type="PROMPT_INJECTION_BLOCKED",
                message=f"Blocked untrusted input injection in research submission by '{agent_id}'",
                detail={"agent_id": agent_id, "source_identifier": source_identifier, "hits": inj_hits},
            )

        # 2. Freshness validation
        freshness_ok = False
        age_ms: int | None = None
        if src_dt is None:
            findings.append("Missing or unparseable source_timestamp (fail-closed freshness)")
        else:
            age_ms = int((ret_dt - src_dt).total_seconds() * 1000.0)
            if age_ms < -5000:
                findings.append(f"Source timestamp skewed into future ({age_ms}ms)")
            elif age_ms > int(max_age_ms):
                findings.append(f"Stale research data: age {age_ms}ms exceeds limit {max_age_ms}ms")
            else:
                freshness_ok = True

        # 3. Conflicting external source claims
        conflict_detected = False
        claims = conflicting_claims or []
        if len(claims) >= 2:
            probs = [float(c["prob"]) for c in claims if isinstance(c, dict) and c.get("prob") is not None]
            stances = {str(c.get("stance")).upper() for c in claims if isinstance(c, dict) and c.get("stance")}
            if len(stances) > 1 or (probs and (max(probs) - min(probs)) >= 0.15):
                conflict_detected = True
                findings.append(
                    f"Conflicting sources detected (probs={probs}, stances={sorted(stances)})"
                )

        # 4. Duplicate market detection across recent validations / snapshots
        duplicate_detected = False
        norm_q = re.sub(r"\s+", " ", (market_question or "").strip().lower())
        norm_mid = market_id.strip()
        with self.store._lock:
            if norm_mid:
                dup_val = self.store._conn.execute(
                    "SELECT COUNT(*) FROM research_validations WHERE json_extract_string(payload_json, '$.market_id') = ?",
                    [norm_mid],
                ).fetchone()[0]
                if int(dup_val or 0) > 0:
                    duplicate_detected = True
                    findings.append(f"Duplicate market evaluation already recorded for market_id '{norm_mid}'")
            if not duplicate_detected and norm_q:
                dup_q = self.store._conn.execute(
                    "SELECT COUNT(*) FROM research_validations WHERE LOWER(json_extract_string(payload_json, '$.market_question')) = ?",
                    [norm_q],
                ).fetchone()[0]
                if int(dup_q or 0) > 0:
                    duplicate_detected = True
                    findings.append("Duplicate normalized market question detected across prior validations")

        # 5. Cross-venue contract equivalence check via existing matcher (when supplied)
        contract_eq_ok = True
        if cross_venue_check:
            poly_mkt = PolymarketBinaryMarket(
                market_id=str(cross_venue_check.get("poly_market_id", market_id or "POLY-CHK")),
                condition_id=str(cross_venue_check.get("condition_id", "0xcond")),
                question=market_question or str(cross_venue_check.get("poly_question", "")),
                slug="chk",
                end_date=str(cross_venue_check.get("poly_end_date") or ""),
                yes_token_id="yes_tok",
                no_token_id="no_tok",
                fees_enabled=bool(cross_venue_check.get("fees_enabled", False)),
                fee_type="taker_dynamic",
                fee_rate=float(cross_venue_check.get("fee_rate", 0.0)),
                fee_exponent=float(cross_venue_check.get("fee_exponent", 1.0)),
                neg_risk=bool(cross_venue_check.get("neg_risk", False)),
                neg_risk_other=bool(cross_venue_check.get("neg_risk_other", False)),
                volume_24hr=1000.0,
                liquidity_usd=5000.0,
                identical_settlement_verified=bool(cross_venue_check.get("poly_binary_verified", True)),
            )
            k_end = str(cross_venue_check.get("kalshi_end_date") or "")
            kalshi_mkt = KalshiBinaryMarket(
                ticker=str(cross_venue_check.get("kalshi_ticker", "KAL-CHK")),
                event_ticker="EVT-CHK",
                series_ticker="SER-CHK",
                title=str(cross_venue_check.get("kalshi_title", market_question)),
                subtitle="",
                yes_sub_title="Yes",
                no_sub_title="No",
                rules_primary=str(cross_venue_check.get("rules_primary", "")),
                rules_secondary="",
                open_time="",
                close_time=k_end,
                expiration_time=k_end,
                updated_time="",
                updated_at_ms=int(time.time() * 1000),
                can_close_early=bool(cross_venue_check.get("can_close_early", False)),
                yes_bid_dollars=0.45,
                yes_ask_dollars=0.46,
                no_bid_dollars=0.54,
                no_ask_dollars=0.55,
                volume_24h=1000.0,
                open_interest=5000.0,
            )
            eq_res = assess_contract_equivalence(
                poly_market=poly_mkt,
                kalshi_market=kalshi_mkt,
                resolution_source_verified=bool(cross_venue_check.get("resolution_source_verified", False)),
                predicate_verified=bool(cross_venue_check.get("predicate_verified", False)),
                polarity_inverted=bool(cross_venue_check.get("polarity_inverted", False)),
                void_rules_compatible=bool(cross_venue_check.get("void_rules_compatible", False)),
                allow_early_close_variance=bool(cross_venue_check.get("allow_early_close_variance", False)),
            )
            contract_eq_ok = eq_res.identical_settlement_verified
            if not contract_eq_ok:
                findings.extend(list(eq_res.mismatch_reasons))

        # 6. Evidence sufficiency & final fail-closed status
        conf_raw = float(confidence)
        if not math.isfinite(conf_raw):
            eff_conf = 0.0
            findings.append("Non-finite confidence value rejected (fail-closed)")
        else:
            eff_conf = round(min(1.0, max(0.0, conf_raw)), 4)
        ev_clean = evidence_summary.strip()
        evidence_sufficient = bool(source_identifier.strip()) and len(ev_clean) >= 20 and eff_conf >= 0.45
        if not evidence_sufficient:
            findings.append(
                "Insufficient evidence or low confidence (<0.45 / summary <20 chars); must return uncertainty"
            )

        if injection_detected:
            status = "REJECTED_INJECTION"
        elif not contract_eq_ok:
            status = "CONTRACT_MISMATCH_BLOCKED"
        elif not freshness_ok:
            status = "STALE_DATA"
        elif conflict_detected:
            status = "CONFLICTING_EVIDENCE"
        elif not evidence_sufficient:
            status = "UNCERTAIN_INSUFFICIENT_EVIDENCE"
        else:
            status = "VERIFIED"

        val_id = f"RVAL-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()
        payload_rec = {
            "market_id": norm_mid,
            "market_question": market_question,
            "age_ms": age_ms,
            "injection_hits": inj_hits,
            "conflicting_claims_count": len(claims),
        }

        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO research_validations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    val_id,
                    agent_id.strip(),
                    source_identifier.strip(),
                    source_timestamp,
                    ret_iso,
                    ev_clean[:500],
                    status,
                    freshness_ok,
                    conflict_detected,
                    duplicate_detected,
                    injection_detected,
                    contract_eq_ok,
                    eff_conf,
                    json.dumps(payload_rec),
                    json.dumps(findings),
                    now_iso,
                ],
            )

        # Feed research quality errors into Stage 3B Learning Core (never self-promoted)
        if status in ("REJECTED_INJECTION", "CONFLICTING_EVIDENCE", "STALE_DATA", "CONTRACT_MISMATCH_BLOCKED"):
            self.learning.record_observation(
                source_type="RESEARCH_QUALITY",
                source_ref_id=val_id,
                agent_id=agent_id.strip(),
                subject_key=f"research:{status.lower()}:{norm_mid or 'unknown'}",
                hypothesis=f"Research validation rejected with {status} from source '{source_identifier}'",
                expected_outcome={"verification_status": "VERIFIED"},
                evidence=[
                    {
                        "source_type": "RESEARCH_VALIDATION",
                        "source_ref_id": val_id,
                        "detail": "; ".join(findings)[:240],
                    }
                ],
            )

        return {
            "validation_id": val_id,
            "agent_id": agent_id.strip(),
            "source_identifier": source_identifier.strip(),
            "source_timestamp": source_timestamp,
            "retrieval_timestamp": ret_iso,
            "evidence_summary": ev_clean,
            "verification_status": status,
            "freshness_ok": freshness_ok,
            "conflict_detected": conflict_detected,
            "duplicate_market_detected": duplicate_detected,
            "injection_detected": injection_detected,
            "contract_equivalence_ok": contract_eq_ok,
            "confidence": eff_conf,
            "payload": payload_rec,
            "findings": findings,
            "created_at": now_iso,
        }


# ======================================================================
# PART 7: Paper Execution Stress Laboratory (All 12 Scenarios)
# ======================================================================
PAPER_STRESS_SCENARIOS: tuple[str, ...] = (
    "PARTIAL_FILL",
    "THIN_LIQUIDITY",
    "SUDDEN_PRICE_MOVEMENT",
    "DELAYED_EXECUTION",
    "STALE_ORDER_BOOKS",
    "HIGH_SLIPPAGE",
    "INCREASED_FEES",
    "SECOND_LEG_EXECUTION_FAILURE",
    "POSITION_IMBALANCE",
    "UNWIND_LOSSES",
    "MARKET_SUSPENSION",
    "SETTLEMENT_MISMATCH",
)


class _StubPolyStressClient:
    def __init__(self, market: PolymarketBinaryMarket | None, yes_snap: PolymarketBookSnapshot, no_snap: PolymarketBookSnapshot) -> None:
        self._market = market
        self._yes = yes_snap
        self._no = no_snap

    async def fetch_market(self, _market_id: str) -> PolymarketBinaryMarket | None:
        return self._market

    async def fetch_order_book(self, token_id: str) -> PolymarketBookSnapshot:
        return self._yes if token_id == "yes_tok" else self._no


class _StubKalshiStressClient:
    def __init__(
        self,
        market: KalshiBinaryMarket | None,
        snap: KalshiBookSnapshot,
        post_latency_snap: KalshiBookSnapshot | None = None,
    ) -> None:
        self._market = market
        self._snap = snap
        self._post_snap = post_latency_snap
        self._calls = 0

    async def fetch_market(self, _ticker: str) -> KalshiBinaryMarket | None:
        return self._market

    async def fetch_order_book(self, _ticker: str, depth: int = 15) -> KalshiBookSnapshot:
        self._calls += 1
        if self._calls > 1 and self._post_snap is not None:
            return self._post_snap
        return self._snap


class PaperStressLab:
    """Exercises real PaperExecutionEngine + DeterministicRiskGate across 12 stress scenarios."""

    def __init__(self, store: DuckDBStore, learning: LearningEngine) -> None:
        self.store = store
        self.learning = learning

    def list_runs(self, limit: int = 60) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM paper_stress_runs ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["metrics"] = json.loads(d.pop("metrics_json", "{}"))
                out.append(d)
            return out

    async def run_scenario_suite(
        self,
        *,
        scenarios: list[str] | None = None,
        target_shares: float = 25.0,
    ) -> dict[str, Any]:
        selected = [s.strip().upper() for s in scenarios] if scenarios else list(PAPER_STRESS_SCENARIOS)
        for s in selected:
            if s not in PAPER_STRESS_SCENARIOS:
                raise ValueError(f"Unsupported paper stress scenario '{s}'. Valid: {PAPER_STRESS_SCENARIOS}")

        suite_results: list[dict[str, Any]] = []
        approved_cnt = 0
        rejected_cnt = 0
        total_sim_pnl = 0.0
        total_unwind_loss = 0.0

        now_ms = int(time.time() * 1000)
        end_iso = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

        for scen in selected:
            # Isolated Risk Gate + PaperExecutionEngine so real blotter exposure isn't distorted
            iso_store = DuckDBStore(db_path=":memory:")
            iso_gate = DeterministicRiskGate(store=iso_store, policy=RiskPolicy())

            poly_mkt = PolymarketBinaryMarket(
                market_id="STRESS-POLY-1",
                condition_id="0xstresscond",
                question="Will benchmark binary event resolve YES?",
                slug="stress-bench",
                end_date=end_iso,
                yes_token_id="yes_tok",
                no_token_id="no_tok",
                fees_enabled=(scen == "INCREASED_FEES"),
                fee_type="taker_dynamic",
                fee_rate=0.0 if scen != "INCREASED_FEES" else 0.05,
                fee_exponent=1.0,
                neg_risk=False,
                neg_risk_other=False,
                volume_24hr=5000.0,
                liquidity_usd=25000.0,
                identical_settlement_verified=True,
            )
            kalshi_mkt = KalshiBinaryMarket(
                ticker="STRESS-KAL-1",
                event_ticker="STRESS-EVT",
                series_ticker="STRESS-SER",
                title="Will benchmark binary event resolve YES?",
                subtitle="",
                yes_sub_title="Yes",
                no_sub_title="No",
                rules_primary="Standard settlement",
                rules_secondary="",
                open_time=end_iso,
                close_time=end_iso,
                expiration_time=end_iso,
                updated_time=end_iso,
                updated_at_ms=now_ms,
                can_close_early=False,
                yes_bid_dollars=0.49,
                yes_ask_dollars=0.52,
                no_bid_dollars=0.46,
                no_ask_dollars=0.48,
                volume_24h=5000.0,
                open_interest=10000.0,
            )

            pair_cfg = {
                "pair_id": f"XPAIR-STRESS-{scen[:8]}",
                "poly_market_id": poly_mkt.market_id,
                "kalshi_ticker": kalshi_mkt.ticker,
                "resolution_source_verified": scen != "SETTLEMENT_MISMATCH",
                "predicate_verified": scen != "SETTLEMENT_MISMATCH",
                "polarity_inverted": False,
                "void_rules_compatible": scen != "SETTLEMENT_MISMATCH",
                "allow_early_close_variance": False,
            }

            # STALE_ORDER_BOOKS simulates a stale VIEW of the books: the data
            # was fetched 25s ago (the book's own last-update timestamp is
            # diagnostics-only and must not drive the gating decision).
            book_ts = now_ms - (25_000 if scen == "STALE_ORDER_BOOKS" else 150)
            fetch_ts = now_ms - (25_000 if scen == "STALE_ORDER_BOOKS" else 0)
            ask_size = 50.0 if scen == "THIN_LIQUIDITY" else 800.0
            yes_ask_px = 0.44
            no_ask_px = 0.48

            poly_yes_asks = (
                (OrderBookLevel(0.44, 10.0), OrderBookLevel(0.56, 40.0))
                if scen == "HIGH_SLIPPAGE"
                else (OrderBookLevel(yes_ask_px, ask_size),)
            )
            poly_yes_bids = (
                (OrderBookLevel(0.28, 800.0),)
                if scen == "UNWIND_LOSSES"
                else (OrderBookLevel(0.42, 800.0),)
            )
            poly_yes_snap = PolymarketBookSnapshot(
                token_id="yes_tok",
                market_condition_id="0xstresscond",
                timestamp_ms=book_ts,
                fetched_at_ms=fetch_ts,
                min_order_size=1.0,
                tick_size=0.01,
                neg_risk=False,
                book=NormalizedOrderBook(bids=poly_yes_bids, asks=poly_yes_asks),
                raw_bids_count=len(poly_yes_bids),
                raw_asks_count=len(poly_yes_asks),
            )
            poly_no_snap = PolymarketBookSnapshot(
                token_id="no_tok",
                market_condition_id="0xstresscond",
                timestamp_ms=book_ts,
                fetched_at_ms=fetch_ts,
                min_order_size=1.0,
                tick_size=0.01,
                neg_risk=False,
                book=NormalizedOrderBook(
                    bids=(OrderBookLevel(0.46, 800.0),),
                    asks=(OrderBookLevel(0.48, ask_size),),
                ),
                raw_bids_count=1,
                raw_asks_count=1,
            )
            kalshi_snap = KalshiBookSnapshot(
                ticker=kalshi_mkt.ticker,
                timestamp_ms=book_ts,
                fetched_at_ms=fetch_ts,
                yes_book=NormalizedOrderBook(
                    bids=(OrderBookLevel(0.49, 800.0),),
                    asks=(OrderBookLevel(0.52, ask_size),),
                ),
                no_book=NormalizedOrderBook(
                    bids=(OrderBookLevel(0.46, 800.0),),
                    asks=(OrderBookLevel(no_ask_px, ask_size),),
                ),
                raw_yes_bids_count=1,
                raw_no_bids_count=1,
            )
            kalshi_post_snap = (
                KalshiBookSnapshot(
                    ticker=kalshi_mkt.ticker,
                    timestamp_ms=book_ts,
                    fetched_at_ms=fetch_ts,
                    yes_book=kalshi_snap.yes_book,
                    no_book=NormalizedOrderBook(
                        bids=(OrderBookLevel(0.46, 800.0),),
                        asks=(OrderBookLevel(no_ask_px, round(max(1.0, float(target_shares) * 0.48), 2)),),
                    ),
                    raw_yes_bids_count=1,
                    raw_no_bids_count=1,
                )
                if scen == "POSITION_IMBALANCE"
                else None
            )

            eff_poly_mkt = None if scen == "MARKET_SUSPENSION" else poly_mkt
            engine = PaperExecutionEngine(
                store=iso_store,
                risk_gate=iso_gate,
                poly_client=_StubPolyStressClient(eff_poly_mkt, poly_yes_snap, poly_no_snap),  # type: ignore[arg-type]
                kalshi_client=_StubKalshiStressClient(kalshi_mkt, kalshi_snap, post_latency_snap=kalshi_post_snap),  # type: ignore[arg-type]
            )

            # Map stress scenario onto PaperExecutionRequest parameters
            exec_scen = "LIVE_BOOKS"
            if scen == "PARTIAL_FILL":
                exec_scen = "PARTIAL_FILL_LEG1"
            elif scen in ("SUDDEN_PRICE_MOVEMENT", "UNWIND_LOSSES"):
                exec_scen = "SECOND_LEG_PRICE_DRIFT_UNWIND"
            elif scen == "SECOND_LEG_EXECUTION_FAILURE":
                exec_scen = "LEG2_REJECTED_UNWIND"

            latency_ms_cfg = 450 if scen == "DELAYED_EXECUTION" else 85
            req = PaperExecutionRequest(
                pair_id=pair_cfg["pair_id"],
                direction="POLY_YES_KALSHI_NO",
                target_shares=float(target_shares),
                slippage_buffer_bps=15.0,
                max_leg_slippage_bps=35.0 if scen == "HIGH_SLIPPAGE" else 50.0,
                leg1_latency_ms=latency_ms_cfg,
                leg2_latency_ms=latency_ms_cfg,
                scenario=exec_scen,  # type: ignore[arg-type]
                partial_fill_ratio=0.4,
            )

            t0 = time.monotonic()
            expected_edge_pct = round((1.0 - (yes_ask_px + no_ask_px)) * 100.0, 4)

            if scen == "MARKET_SUSPENSION":
                # Verify fail-closed rejection when live market suspended / inactive
                try:
                    await engine.execute(req, pair=pair_cfg)
                    exec_out = {"status": "UNEXPECTED_SUCCESS", "approved": True}
                except ValueError as exc:
                    exec_out = {
                        "status": "REJECTED_MARKET_SUSPENDED",
                        "approved": False,
                        "gate_result": {"approved": False, "summary": f"Market suspended/inactive: {exc}"},
                        "orders": [],
                        "pnl_entry": None,
                    }
            else:
                # For THIN_LIQUIDITY, POSITION_IMBALANCE: reduce asks or enforce thin liquidity check
                exec_out = await engine.execute(
                    req,
                    pair=pair_cfg,
                    poly_market=poly_mkt,
                    kalshi_market=kalshi_mkt,
                    post_latency_hook=None if scen != "DELAYED_EXECUTION" else (lambda: asyncio.sleep(0.01)),
                )

            elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
            gate_approved = bool(exec_out.get("approved", False))
            if gate_approved:
                approved_cnt += 1
            else:
                rejected_cnt += 1

            pnl_entry = exec_out.get("pnl_entry") or {}
            orders = exec_out.get("orders") or []
            matched_shares = float(pnl_entry.get("matched_shares", 0.0))
            unwound_shares = float(pnl_entry.get("unwound_shares", 0.0))
            fees_usd = float(pnl_entry.get("fees_usd", 0.0))
            gross_cost = float(pnl_entry.get("gross_cost_usd", 0.0))
            realized_pnl = float(pnl_entry.get("realized_pnl_usd", 0.0))
            locked_edge_usd = float(pnl_entry.get("locked_arb_edge_usd", 0.0))
            unwind_loss = round(max(0.0, -realized_pnl), 4)
            fill_rate = round((matched_shares / float(target_shares)) * 100.0, 2) if gate_approved else 0.0
            realized_edge_pct = (
                round((locked_edge_usd / max(1e-6, matched_shares)) * 100.0, 4)
                if matched_shares > 0
                else (round((realized_pnl / float(target_shares)) * 100.0, 4) if gate_approved else None)
            )
            slippage_usd = round(max(0.0, gross_cost - matched_shares * (yes_ask_px + no_ask_px)), 4) if matched_shares > 0 else 0.0
            rej_reason = None if gate_approved else str((exec_out.get("gate_result") or {}).get("summary", exec_out["status"]))
            sim_latency = round(sum(float(o.get("latency_ms", 0)) for o in orders) or elapsed_ms, 2)

            total_sim_pnl += realized_pnl
            total_unwind_loss += unwind_loss

            # Feed significant stress discrepancies into Stage 3B Learning Engine
            cycle_id: str | None = None
            if (not gate_approved and scen in ("STALE_ORDER_BOOKS", "SETTLEMENT_MISMATCH", "INCREASED_FEES")) or unwind_loss > 0:
                cyc = self.learning.record_observation(
                    source_type="PAPER_STRESS",
                    source_ref_id=f"{scen}:{pair_cfg['pair_id']}",
                    agent_id="risk_intelligence_agent",
                    subject_key=f"paper_stress:{scen.lower()}",
                    hypothesis=f"Stress scenario {scen} impact: status={exec_out['status']}, unwind_loss=${unwind_loss:.4f}",
                    expected_outcome={"expected_edge_pct": expected_edge_pct, "approved": True},
                    evidence=[
                        {
                            "source_type": "PAPER_STRESS_SIM",
                            "source_ref_id": pair_cfg["pair_id"],
                            "detail": f"status={exec_out['status']}, fees=${fees_usd:.4f}, pnl=${realized_pnl:.4f}",
                        }
                    ],
                )
                cycle_id = cyc["cycle_id"]

            stress_id = f"PSTR-{uuid.uuid4().hex[:10].upper()}"
            now_iso = utc_now_iso()
            metrics_blob = {
                "matched_shares": matched_shares,
                "unwound_shares": unwound_shares,
                "locked_arb_edge_usd": locked_edge_usd,
                "orders_count": len(orders),
                "execution_status": exec_out["status"],
            }

            with self.store._lock:
                self.store._conn.execute(
                    """
                    INSERT INTO paper_stress_runs VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        stress_id,
                        scen,
                        pair_cfg["pair_id"],
                        str(exec_out["status"]),
                        True,
                        gate_approved,
                        expected_edge_pct,
                        realized_edge_pct,
                        realized_pnl,
                        fees_usd,
                        slippage_usd,
                        fill_rate,
                        sim_latency,
                        unwind_loss,
                        rej_reason,
                        cycle_id,
                        json.dumps(metrics_blob),
                        now_iso,
                    ],
                )
            iso_store.close()

            suite_results.append(
                {
                    "stress_id": stress_id,
                    "scenario_name": scen,
                    "pair_id": pair_cfg["pair_id"],
                    "status": str(exec_out["status"]),
                    "proven_simulated": True,
                    "risk_gate_approved": gate_approved,
                    "expected_edge_pct": expected_edge_pct,
                    "realized_simulated_edge_pct": realized_edge_pct,
                    "simulated_pnl_usd": realized_pnl,
                    "fees_usd": fees_usd,
                    "slippage_usd": slippage_usd,
                    "fill_rate_pct": fill_rate,
                    "latency_ms": sim_latency,
                    "unwind_loss_usd": unwind_loss,
                    "rejection_reason": rej_reason,
                    "learning_cycle_id": cycle_id,
                    "metrics": metrics_blob,
                    "created_at": now_iso,
                }
            )

        total_cnt = len(suite_results)
        rej_rate_pct = round((rejected_cnt / total_cnt) * 100.0, 2) if total_cnt > 0 else 0.0
        return {
            "provenance": "SIMULATED",
            "live_trading_enabled": False,
            "scenarios_executed": total_cnt,
            "approved_count": approved_cnt,
            "rejected_count": rejected_cnt,
            "risk_gate_rejection_rate_pct": rej_rate_pct,
            "total_simulated_pnl_usd": round(total_sim_pnl, 4),
            "total_unwind_loss_usd": round(total_unwind_loss, 4),
            "results": suite_results,
        }


# ======================================================================
# PART 8: Adversarial Research Review (Proposer -> Challenger -> Arbiter)
# ======================================================================
class AdversarialReviewEngine:
    """Enforces 3-role Proposer/Challenger/Arbiter review preventing self-approval and unverified claims."""

    DEFAULT_CHALLENGER = "market_intelligence_agent"
    DEFAULT_ARBITER = "risk_intelligence_agent"

    def __init__(
        self,
        store: DuckDBStore,
        memory: StructuredMemoryManager,
        learning: LearningEngine,
    ) -> None:
        self.store = store
        self.memory = memory
        self.learning = learning

    def list_reviews(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM adversarial_reviews ORDER BY resolved_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(zip(cols, r))
                d["proposer_evidence"] = json.loads(d.pop("proposer_evidence_json", "[]"))
                d["opposing_evidence"] = json.loads(d.pop("opposing_evidence_json", "[]"))
                d["checks"] = json.loads(d.pop("checks_json", "[]"))
                out.append(d)
            return out

    def run_review(
        self,
        *,
        subject_key: str,
        conclusion_agent: str,
        conclusion_summary: str,
        proposer_evidence: list[dict[str, Any]] | None = None,
        challenger_agent: str | None = None,
        arbiter_agent: str | None = None,
        metrics: dict[str, Any] | None = None,
        opposing_evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        proposer = conclusion_agent.strip()
        challenger = (
            challenger_agent.strip()
            if challenger_agent
            else (
                "arbitrage_research_agent"
                if proposer.lower() == self.DEFAULT_CHALLENGER
                else self.DEFAULT_CHALLENGER
            )
        )
        arbiter = (
            arbiter_agent.strip()
            if arbiter_agent
            else (
                "learning_agent"
                if proposer.lower() == self.DEFAULT_ARBITER or challenger.lower() == self.DEFAULT_ARBITER
                else self.DEFAULT_ARBITER
            )
        )

        # Anti-self-approval gate
        if challenger.lower() == proposer.lower():
            raise PermissionError("Adversarial Challenger must be distinct from conclusion_agent")
        if arbiter.lower() in (proposer.lower(), challenger.lower()):
            raise PermissionError("Adversarial Arbiter must be independent of Proposer and Challenger")

        prop_ev = [e for e in (proposer_evidence or []) if isinstance(e, dict)]
        opp_ev = [e for e in (opposing_evidence or []) if isinstance(e, dict)]
        m = dict(metrics or {})

        # Challenger checks all 8 adversarial defect vectors
        checks: list[dict[str, Any]] = []
        weak_evidence = len(prop_ev) == 0 or len(conclusion_summary.strip()) < 15
        checks.append({
            "check_id": "WEAK_OR_MISSING_EVIDENCE",
            "failed": weak_evidence,
            "detail": f"Proposer supplied {len(prop_ev)} evidence items"
            if not weak_evidence
            else "Missing verifiable proposer evidence or summary too brief",
        })

        claimed_prob = m.get("claimed_prob")
        extreme_unverified = (
            claimed_prob is not None
            and (float(claimed_prob) >= 0.95 or float(claimed_prob) <= 0.05)
            and len(prop_ev) < 2
        )
        checks.append({
            "check_id": "UNVERIFIED_EXTREME_PROBABILITY",
            "failed": extreme_unverified,
            "detail": "Extreme tail probability requires >=2 independent evidence sources"
            if extreme_unverified
            else "Probability confidence properly supported",
        })

        conflicting_sources = int(m.get("conflicting_sources_count", 0)) > 0 or len(opp_ev) > 0
        checks.append({
            "check_id": "CONFLICTING_MARKET_DATA",
            "failed": conflicting_sources,
            "detail": f"Opposing evidence ({len(opp_ev)}) or conflicting sources present"
            if conflicting_sources
            else "No conflicting source claims detected",
        })

        contract_mismatch = m.get("settlement_verified") is False
        checks.append({
            "check_id": "CONTRACT_EQUIVALENCE_MISMATCH",
            "failed": contract_mismatch,
            "detail": "Cross-venue settlement equivalence not verified (fail-closed)"
            if contract_mismatch
            else "Contract equivalence satisfied or N/A",
        })

        missing_settlement = m.get("settlement_conditions_present") is False
        checks.append({
            "check_id": "MISSING_SETTLEMENT_CONDITIONS",
            "failed": missing_settlement,
            "detail": "Primary resolution oracle/void criteria omitted"
            if missing_settlement
            else "Settlement rules accounted for",
        })

        part_pct = float(m.get("liquidity_participation_pct", 0.0))
        hidden_liquidity = part_pct > 10.0 or bool(m.get("thin_liquidity_ignored", False))
        checks.append({
            "check_id": "HIDDEN_LIQUIDITY_CONSTRAINT",
            "failed": hidden_liquidity,
            "detail": f"Participation {part_pct:.2f}% > 10% limit or thin book ignored"
            if hidden_liquidity
            else "Order book liquidity capacity within bounds",
        })

        age_ms = m.get("data_age_ms")
        stale_data = age_ms is not None and int(age_ms) > 15_000
        checks.append({
            "check_id": "STALE_DATA",
            "failed": stale_data,
            "detail": f"Data age {age_ms}ms > 15000ms limit"
            if stale_data
            else "Data timestamp freshness within 15s threshold",
        })

        gross_e = m.get("gross_edge_pct")
        net_e = m.get("net_edge_pct")
        misleading_math = bool(m.get("fees_omitted", False)) or (
            gross_e is not None and net_e is not None and float(net_e) > float(gross_e)
        )
        checks.append({
            "check_id": "MISLEADING_ARBITRAGE_MATH",
            "failed": misleading_math,
            "detail": "Arbitrage math omits taker fees/slippage or net > gross"
            if misleading_math
            else "Fee & slippage arithmetic verified consistent",
        })

        hard_disproved = any(
            c["failed"]
            for c in checks
            if c["check_id"]
            in (
                "CONFLICTING_MARKET_DATA",
                "CONTRACT_EQUIVALENCE_MISMATCH",
                "HIDDEN_LIQUIDITY_CONSTRAINT",
                "STALE_DATA",
                "MISLEADING_ARBITRAGE_MATH",
            )
        )
        uncertain_unverified = any(
            c["failed"]
            for c in checks
            if c["check_id"]
            in ("WEAK_OR_MISSING_EVIDENCE", "UNVERIFIED_EXTREME_PROBABILITY", "MISSING_SETTLEMENT_CONDITIONS")
        )

        failed_labels = [c["check_id"] for c in checks if c["failed"]]
        if hard_disproved:
            verdict = "DISPROVED"
            challenge_notes = f"Challenger disproved claim on: {', '.join(failed_labels)}"
            for fl in failed_labels:
                opp_ev.append(
                    {
                        "source_type": "ADVERSARIAL_CHALLENGE",
                        "source_ref_id": fl,
                        "detail": next(c["detail"] for c in checks if c["check_id"] == fl),
                    }
                )
        elif uncertain_unverified:
            verdict = "UNVERIFIED"
            challenge_notes = f"Insufficient proof / unresolved uncertainty on: {', '.join(failed_labels)}"
        else:
            verdict = "VERIFIED"
            challenge_notes = "All 8 adversarial challenger checks passed without contradicting evidence."

        review_id = f"AREV-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()
        learning_cycle_id: str | None = None

        if verdict == "DISPROVED":
            cyc = self.learning.record_observation(
                source_type="ADVERSARIAL_REVIEW",
                source_ref_id=review_id,
                agent_id=proposer,
                subject_key=f"adversarial:{subject_key.strip().lower()}",
                hypothesis=conclusion_summary.strip()[:240],
                expected_outcome={"verdict": "VERIFIED"},
                evidence=opp_ev,
            )
            learning_cycle_id = cyc["cycle_id"]

        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO adversarial_reviews VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    review_id,
                    subject_key.strip().lower(),
                    proposer,
                    conclusion_summary.strip(),
                    json.dumps(prop_ev),
                    challenger,
                    challenge_notes,
                    json.dumps(opp_ev),
                    arbiter,
                    verdict,
                    json.dumps(checks),
                    learning_cycle_id,
                    now_iso,
                    now_iso,
                ],
            )

        return {
            "review_id": review_id,
            "subject_key": subject_key.strip().lower(),
            "conclusion_agent": proposer,
            "conclusion_summary": conclusion_summary.strip(),
            "proposer_evidence": prop_ev,
            "challenger_agent": challenger,
            "challenge_notes": challenge_notes,
            "opposing_evidence": opp_ev,
            "arbiter_agent": arbiter,
            "verdict": verdict,
            "checks": checks,
            "learning_cycle_id": learning_cycle_id,
            "created_at": now_iso,
            "resolved_at": now_iso,
        }
