"""Thread-safe DuckDB analytical and audit persistence for The Certainty Stack."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DuckDBStore:
    """Thread-safe DuckDB store for market snapshots, arbitrage opportunities, audit logs, and agent memory."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = db_path or os.environ.get(
            "CERTAINTY_DUCKDB_PATH", "data/certainty_stack.duckdb"
        )
        self.db_path = str(resolved)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = duckdb.connect(self.db_path)
        self._init_schema()
        self._seed_initial_memory_if_empty()
        self._backfill_structured_memory_from_legacy()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    snapshot_id VARCHAR PRIMARY KEY,
                    scan_id VARCHAR NOT NULL,
                    market_id VARCHAR NOT NULL,
                    condition_id VARCHAR NOT NULL,
                    question VARCHAR NOT NULL,
                    slug VARCHAR NOT NULL,
                    yes_token_id VARCHAR NOT NULL,
                    no_token_id VARCHAR NOT NULL,
                    yes_best_ask DOUBLE,
                    no_best_ask DOUBLE,
                    yes_best_bid DOUBLE,
                    no_best_bid DOUBLE,
                    yes_ask_depth_shares DOUBLE NOT NULL,
                    no_ask_depth_shares DOUBLE NOT NULL,
                    fee_rate DOUBLE NOT NULL,
                    fees_enabled BOOLEAN NOT NULL,
                    neg_risk BOOLEAN NOT NULL,
                    clob_timestamp_ms BIGINT NOT NULL,
                    book_payload_json VARCHAR NOT NULL,
                    ingested_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
                    opp_id VARCHAR PRIMARY KEY,
                    scan_id VARCHAR NOT NULL,
                    market_id VARCHAR NOT NULL,
                    condition_id VARCHAR NOT NULL,
                    question VARCHAR NOT NULL,
                    slug VARCHAR NOT NULL,
                    venue VARCHAR NOT NULL,
                    target_shares DOUBLE NOT NULL,
                    yes_vwap DOUBLE,
                    no_vwap DOUBLE,
                    combined_raw_cost DOUBLE,
                    taker_fees_usd DOUBLE NOT NULL,
                    slippage_buffer_usd DOUBLE NOT NULL,
                    total_executable_cost DOUBLE,
                    guaranteed_payout DOUBLE NOT NULL,
                    gross_edge_pct DOUBLE,
                    net_edge_pct DOUBLE,
                    liquidity_participation_pct DOUBLE NOT NULL,
                    leg_imbalance_ratio DOUBLE NOT NULL,
                    executable BOOLEAN NOT NULL,
                    risk_gate_approved BOOLEAN NOT NULL,
                    risk_gate_reason VARCHAR NOT NULL,
                    data_age_ms BIGINT NOT NULL,
                    evaluated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_gate_audit (
                    audit_id VARCHAR PRIMARY KEY,
                    request_id VARCHAR NOT NULL,
                    venue VARCHAR NOT NULL,
                    strategy_tier VARCHAR NOT NULL,
                    execution_mode VARCHAR NOT NULL,
                    market_id VARCHAR NOT NULL,
                    requested_notional_usd DOUBLE NOT NULL,
                    net_edge_pct DOUBLE NOT NULL,
                    liquidity_participation_pct DOUBLE NOT NULL,
                    data_age_ms BIGINT NOT NULL,
                    decision VARCHAR NOT NULL,
                    failed_rules_json VARCHAR NOT NULL,
                    kill_switch_active BOOLEAN NOT NULL,
                    summary VARCHAR NOT NULL,
                    decided_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_memory (
                    memory_id VARCHAR PRIMARY KEY,
                    category VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    summary VARCHAR NOT NULL,
                    evidence_json VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_state (
                    key VARCHAR PRIMARY KEY,
                    value_json VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cross_venue_pairs (
                    pair_id VARCHAR PRIMARY KEY,
                    poly_market_id VARCHAR NOT NULL,
                    poly_question VARCHAR NOT NULL,
                    kalshi_ticker VARCHAR NOT NULL,
                    kalshi_title VARCHAR NOT NULL,
                    resolution_source_verified BOOLEAN NOT NULL,
                    predicate_verified BOOLEAN NOT NULL,
                    polarity_inverted BOOLEAN NOT NULL,
                    void_rules_compatible BOOLEAN NOT NULL,
                    allow_early_close_variance BOOLEAN NOT NULL,
                    identical_settlement_verified BOOLEAN NOT NULL,
                    settlement_status VARCHAR NOT NULL,
                    mismatch_reasons_json VARCHAR NOT NULL,
                    cutoff_delta_hours DOUBLE,
                    notes VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cross_venue_opportunities (
                    xopp_id VARCHAR PRIMARY KEY,
                    scan_id VARCHAR NOT NULL,
                    pair_id VARCHAR NOT NULL,
                    poly_market_id VARCHAR NOT NULL,
                    kalshi_ticker VARCHAR NOT NULL,
                    question VARCHAR NOT NULL,
                    direction VARCHAR NOT NULL,
                    leg1_venue VARCHAR NOT NULL,
                    leg1_outcome VARCHAR NOT NULL,
                    leg2_venue VARCHAR NOT NULL,
                    leg2_outcome VARCHAR NOT NULL,
                    target_shares DOUBLE NOT NULL,
                    leg1_vwap DOUBLE,
                    leg2_vwap DOUBLE,
                    combined_raw_cost DOUBLE,
                    total_fees_usd DOUBLE NOT NULL,
                    slippage_buffer_usd DOUBLE NOT NULL,
                    total_executable_cost DOUBLE,
                    gross_edge_pct DOUBLE,
                    net_edge_pct DOUBLE,
                    liquidity_participation_pct DOUBLE NOT NULL,
                    leg_imbalance_ratio DOUBLE NOT NULL,
                    executable BOOLEAN NOT NULL,
                    identical_settlement_verified BOOLEAN NOT NULL,
                    settlement_status VARCHAR NOT NULL,
                    risk_gate_approved BOOLEAN NOT NULL,
                    risk_gate_reason VARCHAR NOT NULL,
                    risk_audit_id VARCHAR NOT NULL,
                    data_age_ms BIGINT NOT NULL,
                    route_a_net_edge_pct DOUBLE,
                    route_b_net_edge_pct DOUBLE,
                    provenance VARCHAR NOT NULL,
                    evaluated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id VARCHAR PRIMARY KEY,
                    execution_id VARCHAR NOT NULL,
                    pair_id VARCHAR NOT NULL,
                    audit_id VARCHAR NOT NULL,
                    leg_number INTEGER NOT NULL,
                    venue VARCHAR NOT NULL,
                    market_ref VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    outcome VARCHAR NOT NULL,
                    order_type VARCHAR NOT NULL,
                    requested_shares DOUBLE NOT NULL,
                    filled_shares DOUBLE NOT NULL,
                    limit_price DOUBLE NOT NULL,
                    avg_fill_price DOUBLE,
                    fees_usd DOUBLE NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    status VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    provenance VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_fills (
                    fill_id VARCHAR PRIMARY KEY,
                    execution_id VARCHAR NOT NULL,
                    order_id VARCHAR NOT NULL,
                    venue VARCHAR NOT NULL,
                    market_ref VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    outcome VARCHAR NOT NULL,
                    shares DOUBLE NOT NULL,
                    fill_price DOUBLE NOT NULL,
                    notional_usd DOUBLE NOT NULL,
                    fee_usd DOUBLE NOT NULL,
                    is_unwind BOOLEAN NOT NULL,
                    provenance VARCHAR NOT NULL,
                    filled_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_positions (
                    position_id VARCHAR PRIMARY KEY,
                    execution_id VARCHAR NOT NULL,
                    pair_id VARCHAR NOT NULL,
                    venue VARCHAR NOT NULL,
                    market_ref VARCHAR NOT NULL,
                    outcome VARCHAR NOT NULL,
                    shares DOUBLE NOT NULL,
                    entry_price DOUBLE NOT NULL,
                    notional_usd DOUBLE NOT NULL,
                    locked_payout_usd DOUBLE NOT NULL,
                    status VARCHAR NOT NULL,
                    provenance VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_pnl_ledger (
                    ledger_id VARCHAR PRIMARY KEY,
                    execution_id VARCHAR NOT NULL,
                    pair_id VARCHAR NOT NULL,
                    event_type VARCHAR NOT NULL,
                    outcome_status VARCHAR NOT NULL,
                    matched_shares DOUBLE NOT NULL,
                    unwound_shares DOUBLE NOT NULL,
                    gross_cost_usd DOUBLE NOT NULL,
                    fees_usd DOUBLE NOT NULL,
                    realized_pnl_usd DOUBLE NOT NULL,
                    locked_arb_edge_usd DOUBLE NOT NULL,
                    notes VARCHAR NOT NULL,
                    provenance VARCHAR NOT NULL,
                    recorded_at VARCHAR NOT NULL
                )
                """
            )
            # Phase 3 Stage 3B additive tables (preserve all Phase 1 & Phase 2 tables untouched)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    memory_id VARCHAR PRIMARY KEY,
                    memory_key VARCHAR NOT NULL,
                    memory_tier VARCHAR NOT NULL,
                    category VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    summary VARCHAR NOT NULL,
                    content_json VARCHAR NOT NULL,
                    evidence_json VARCHAR NOT NULL,
                    tags_json VARCHAR NOT NULL,
                    confidence DOUBLE NOT NULL,
                    source_agent VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    version INTEGER NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    contradicts_id VARCHAR,
                    supersedes_id VARCHAR,
                    retention_policy VARCHAR NOT NULL,
                    expires_at VARCHAR,
                    last_applied_at VARCHAR,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_versions (
                    version_id VARCHAR PRIMARY KEY,
                    memory_id VARCHAR NOT NULL,
                    version INTEGER NOT NULL,
                    title VARCHAR NOT NULL,
                    summary VARCHAR NOT NULL,
                    content_json VARCHAR NOT NULL,
                    evidence_json VARCHAR NOT NULL,
                    confidence DOUBLE NOT NULL,
                    status VARCHAR NOT NULL,
                    change_reason VARCHAR NOT NULL,
                    changed_by VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_cycles (
                    cycle_id VARCHAR PRIMARY KEY,
                    stage VARCHAR NOT NULL,
                    source_type VARCHAR NOT NULL,
                    source_ref_id VARCHAR NOT NULL,
                    agent_id VARCHAR NOT NULL,
                    subject_key VARCHAR NOT NULL,
                    hypothesis VARCHAR NOT NULL,
                    expected_outcome_json VARCHAR NOT NULL,
                    decision_summary VARCHAR NOT NULL,
                    actual_outcome_json VARCHAR NOT NULL,
                    outcome_matched BOOLEAN,
                    review_notes VARCHAR NOT NULL,
                    evidence_json VARCHAR NOT NULL,
                    candidate_memory_id VARCHAR,
                    promoted_memory_id VARCHAR,
                    regression_case_id VARCHAR,
                    applied_flag BOOLEAN NOT NULL,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generated_regression_cases (
                    case_id VARCHAR PRIMARY KEY,
                    source_memory_id VARCHAR,
                    source_cycle_id VARCHAR,
                    source_ref_id VARCHAR NOT NULL,
                    category VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    description VARCHAR NOT NULL,
                    input_fixture_json VARCHAR NOT NULL,
                    expected_invariant_json VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    last_run_status VARCHAR,
                    last_run_detail VARCHAR,
                    last_run_at VARCHAR,
                    created_at VARCHAR NOT NULL
                )
                """
            )
            # Phase 3 Stage 3C additive orchestration & recovery tables
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id VARCHAR PRIMARY KEY,
                    dedup_key VARCHAR NOT NULL,
                    task_type VARCHAR NOT NULL,
                    assigned_agent VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    priority INTEGER NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    result_json VARCHAR NOT NULL,
                    depends_on_json VARCHAR NOT NULL,
                    worker_id VARCHAR,
                    lease_token VARCHAR,
                    lease_expires_at VARCHAR,
                    last_heartbeat_at VARCHAR,
                    timeout_seconds DOUBLE NOT NULL,
                    started_at VARCHAR,
                    attempt_count INTEGER NOT NULL,
                    max_retries INTEGER NOT NULL,
                    recovery_attempts INTEGER NOT NULL,
                    max_recovery_attempts INTEGER NOT NULL,
                    last_blocker_id VARCHAR,
                    escalation_reason VARCHAR,
                    created_by VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_registry (
                    worker_id VARCHAR PRIMARY KEY,
                    agent_role VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    current_task_id VARCHAR,
                    permitted_actions_json VARCHAR NOT NULL,
                    last_heartbeat_at VARCHAR NOT NULL,
                    tasks_completed INTEGER NOT NULL,
                    tasks_failed INTEGER NOT NULL,
                    spawned_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blocker_events (
                    blocker_id VARCHAR PRIMARY KEY,
                    task_id VARCHAR NOT NULL,
                    agent_id VARCHAR NOT NULL,
                    worker_id VARCHAR,
                    blocker_class VARCHAR NOT NULL,
                    error_message VARCHAR NOT NULL,
                    memory_hits_json VARCHAR NOT NULL,
                    candidate_strategies_json VARCHAR NOT NULL,
                    selected_strategy VARCHAR,
                    recovery_action VARCHAR,
                    verification_detail VARCHAR,
                    outcome VARCHAR NOT NULL,
                    learning_cycle_id VARCHAR,
                    regression_case_id VARCHAR,
                    created_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_activity_log (
                    activity_id VARCHAR PRIMARY KEY,
                    actor VARCHAR NOT NULL,
                    agent_role VARCHAR NOT NULL,
                    task_id VARCHAR,
                    worker_id VARCHAR,
                    event_type VARCHAR NOT NULL,
                    summary VARCHAR NOT NULL,
                    detail_json VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                )
                """
            )

    def _seed_initial_memory_if_empty(self) -> None:
        with self._lock:
            count_row = self._conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()
            if count_row and count_row[0] > 0:
                return
            initial_lessons = [
                (
                    "MEM-PHASE0-001",
                    "MARKET_DATA_ERROR",
                    "Polymarket CLOB V2 blocks default Python urllib User-Agent (HTTP 403)",
                    "Live Phase 0 probe showed clob.polymarket.com/book returns HTTP 403 to Python-urllib/3.10. Ingestion adapter uses httpx with explicit research User-Agent and bounded timeouts.",
                    json.dumps({"endpoint": "https://clob.polymarket.com/book", "status_code": 403, "mitigation": "httpx custom User-Agent"}),
                    "VERIFIED_LESSON",
                    utc_now_iso(),
                ),
                (
                    "MEM-PHASE0-002",
                    "MARKET_DATA_INVARIANT",
                    "Polymarket CLOB /book returns bids ascending and asks descending",
                    "Live CLOB V2 book arrays are ordered worst-price-first (bids from 0.001 upward, asks from 0.999 downward). Book normalizer explicitly sorts bids descending and asks ascending before VWAP calculation.",
                    json.dumps({"endpoint": "https://clob.polymarket.com/book", "invariant": "sort_bids_desc_asks_asc"}),
                    "VERIFIED_LESSON",
                    utc_now_iso(),
                ),
                (
                    "MEM-PHASE0-003",
                    "FEE_MODEL",
                    "Polymarket Gamma feeSchedule specifies dynamic taker-only curve",
                    "Verified live Gamma feeSchedule: rate * shares * p * (1 - p) for taker orders when feesEnabled=true; makers pay 0%.",
                    json.dumps({"formula": "shares * rate * p * (1 - p)", "takerOnly": True}),
                    "VERIFIED_LESSON",
                    utc_now_iso(),
                ),
            ]
            self._conn.executemany(
                "INSERT INTO agent_memory VALUES (?, ?, ?, ?, ?, ?, ?)",
                initial_lessons,
            )

    def _backfill_structured_memory_from_legacy(self) -> None:
        """Idempotently backfill existing agent_memory rows into memory_entries without inventing evidence."""
        tier_map = {
            "FEE_MODEL": ("SEMANTIC", "PERMANENT", 0.95),
            "MARKET_DATA_INVARIANT": ("SEMANTIC", "PERMANENT", 0.95),
            "MARKET_DATA_ERROR": ("PROCEDURAL", "PERMANENT", 0.90),
            "BLOCKER_ESCALATION": ("EPISODIC", "STANDARD_90D", 0.70),
            "RISK_GOVERNANCE": ("EPISODIC", "PERMANENT", 0.95),
            "CALIBRATION_NOTE": ("AGENT_PERF", "STANDARD_90D", 0.75),
        }
        with self._lock:
            legacy_rows = self._conn.execute(
                "SELECT memory_id, category, title, summary, evidence_json, status, created_at FROM agent_memory"
            ).fetchall()
            for memory_id, category, title, summary, evidence_json, status, created_at in legacy_rows:
                exists = self._conn.execute(
                    "SELECT 1 FROM memory_entries WHERE memory_id = ?",
                    [memory_id],
                ).fetchone()
                if exists:
                    continue

                try:
                    raw_ev = json.loads(evidence_json or "{}")
                except (TypeError, ValueError):
                    raw_ev = {}

                # Preserve exact legacy evidence without fabricating anything
                evidence_list: list[dict[str, Any]] = []
                if isinstance(raw_ev, list):
                    evidence_list = [e for e in raw_ev if isinstance(e, dict)]
                elif isinstance(raw_ev, dict) and raw_ev:
                    evidence_list = [
                        {
                            "source_type": "LEGACY_AGENT_MEMORY",
                            "source_ref_id": memory_id,
                            "detail": f"Preserved Phase 0/1 evidence for {category}",
                            "payload": raw_ev,
                        }
                    ]

                memory_tier, retention_policy, default_conf = tier_map.get(
                    category, ("EPISODIC", "STANDARD_90D", 0.65)
                )
                norm_status = status
                # Do not treat an empty-evidence legacy record as a verified lesson
                if status == "VERIFIED_LESSON" and not evidence_list:
                    norm_status = "CANDIDATE"

                slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(title)).strip("-")
                while "--" in slug:
                    slug = slug.replace("--", "-")
                memory_key = f"{memory_tier.lower()}:{str(category).lower()}:{slug[:64]}"
                content_obj = {
                    "legacy_memory_id": memory_id,
                    "subject_key": memory_key,
                    "raw_evidence_present": bool(evidence_list),
                }
                tags_obj = [str(category).lower(), "backfilled"]
                if str(memory_id).startswith("MEM-PHASE0"):
                    tags_obj.append("phase0")

                content_str = json.dumps(content_obj)
                ev_str = json.dumps(evidence_list)
                tags_str = json.dumps(tags_obj)

                self._conn.execute(
                    """
                    INSERT INTO memory_entries VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        memory_id,
                        memory_key,
                        memory_tier,
                        category,
                        title,
                        summary,
                        content_str,
                        ev_str,
                        tags_str,
                        default_conf if evidence_list else 0.4,
                        "legacy_backfill",
                        norm_status,
                        1,
                        1,
                        None,
                        None,
                        retention_policy,
                        None,
                        None,
                        created_at,
                        created_at,
                    ],
                )
                self._conn.execute(
                    """
                    INSERT INTO memory_versions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        f"MVER-{uuid.uuid4().hex[:10].upper()}",
                        memory_id,
                        1,
                        title,
                        summary,
                        content_str,
                        ev_str,
                        default_conf if evidence_list else 0.4,
                        norm_status,
                        "Initial backfill from legacy agent_memory preserving original evidence",
                        "legacy_backfill",
                        created_at,
                    ],
                )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM system_state WHERE key = ?", [key]
            ).fetchone()
            if not row:
                return default
            return json.loads(row[0])

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            payload = json.dumps(value)
            now = utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO system_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                [key, payload, now],
            )

    def is_kill_switch_active(self) -> bool:
        val = self.get_state("kill_switch_active", {"active": False})
        if isinstance(val, dict):
            return bool(val.get("active", False))
        return bool(val)

    def set_kill_switch(self, active: bool, reason: str, actor: str = "operator") -> dict[str, Any]:
        payload = {
            "active": bool(active),
            "reason": reason,
            "actor": actor,
            "updated_at": utc_now_iso(),
        }
        self.set_state("kill_switch_active", payload)
        return payload

    def record_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO market_snapshots VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    snapshot["snapshot_id"],
                    snapshot["scan_id"],
                    snapshot["market_id"],
                    snapshot["condition_id"],
                    snapshot["question"],
                    snapshot["slug"],
                    snapshot["yes_token_id"],
                    snapshot["no_token_id"],
                    snapshot.get("yes_best_ask"),
                    snapshot.get("no_best_ask"),
                    snapshot.get("yes_best_bid"),
                    snapshot.get("no_best_bid"),
                    float(snapshot["yes_ask_depth_shares"]),
                    float(snapshot["no_ask_depth_shares"]),
                    float(snapshot["fee_rate"]),
                    bool(snapshot["fees_enabled"]),
                    bool(snapshot["neg_risk"]),
                    int(snapshot["clob_timestamp_ms"]),
                    json.dumps(snapshot.get("book_payload", {})),
                    snapshot["ingested_at"],
                ],
            )

    def record_opportunity(self, opp: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO arbitrage_opportunities VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    opp["opp_id"],
                    opp["scan_id"],
                    opp["market_id"],
                    opp["condition_id"],
                    opp["question"],
                    opp["slug"],
                    opp["venue"],
                    float(opp["target_shares"]),
                    opp.get("yes_vwap"),
                    opp.get("no_vwap"),
                    opp.get("combined_raw_cost"),
                    float(opp["taker_fees_usd"]),
                    float(opp["slippage_buffer_usd"]),
                    opp.get("total_executable_cost"),
                    float(opp["guaranteed_payout"]),
                    opp.get("gross_edge_pct"),
                    opp.get("net_edge_pct"),
                    float(opp["liquidity_participation_pct"]),
                    float(opp["leg_imbalance_ratio"]),
                    bool(opp["executable"]),
                    bool(opp["risk_gate_approved"]),
                    opp["risk_gate_reason"],
                    int(opp["data_age_ms"]),
                    opp["evaluated_at"],
                ],
            )

    def record_risk_audit(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO risk_gate_audit VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    entry["audit_id"],
                    entry["request_id"],
                    entry["venue"],
                    entry["strategy_tier"],
                    entry["execution_mode"],
                    entry["market_id"],
                    float(entry["requested_notional_usd"]),
                    float(entry["net_edge_pct"]),
                    float(entry["liquidity_participation_pct"]),
                    int(entry["data_age_ms"]),
                    entry["decision"],
                    json.dumps(entry["failed_rules"]),
                    bool(entry["kill_switch_active"]),
                    entry["summary"],
                    entry["decided_at"],
                ],
            )

    def add_memory(
        self,
        category: str,
        title: str,
        summary: str,
        evidence: dict[str, Any] | None = None,
        status: str = "RECORDED",
    ) -> dict[str, Any]:
        memory_id = f"MEM-{uuid.uuid4().hex[:10].upper()}"
        created_at = utc_now_iso()
        evidence_obj = evidence or {}
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_memory VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    memory_id,
                    category,
                    title,
                    summary,
                    json.dumps(evidence_obj),
                    status,
                    created_at,
                ],
            )
        return {
            "memory_id": memory_id,
            "category": category,
            "title": title,
            "summary": summary,
            "evidence": evidence_obj,
            "status": status,
            "created_at": created_at,
        }

    def list_opportunities(self, limit: int = 100, latest_scan_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if latest_scan_only:
                rows = self._conn.execute(
                    """
                    WITH latest AS (
                        SELECT scan_id FROM arbitrage_opportunities
                        ORDER BY evaluated_at DESC LIMIT 1
                    )
                    SELECT o.* FROM arbitrage_opportunities o
                    JOIN latest l ON o.scan_id = l.scan_id
                    ORDER BY COALESCE(o.net_edge_pct, -999.0) DESC
                    LIMIT ?
                    """,
                    [limit],
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM arbitrage_opportunities
                    ORDER BY evaluated_at DESC, COALESCE(net_edge_pct, -999.0) DESC
                    LIMIT ?
                    """,
                    [limit],
                ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def get_latest_snapshot_for_market(self, market_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM market_snapshots
                WHERE market_id = ?
                ORDER BY ingested_at DESC
                LIMIT 1
                """,
                [market_id],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self._conn.description]
            record = dict(zip(cols, row))
            record["book_payload"] = json.loads(record.pop("book_payload_json", "{}"))
            return record

    def list_risk_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM risk_gate_audit ORDER BY decided_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            result = []
            for r in rows:
                d = dict(zip(cols, r))
                d["failed_rules"] = json.loads(d.pop("failed_rules_json", "[]"))
                result.append(d)
            return result

    def list_memory(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_memory ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            result = []
            for r in rows:
                d = dict(zip(cols, r))
                d["evidence"] = json.loads(d.pop("evidence_json", "{}"))
                result.append(d)
            return result

    def get_historical_edge_series(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    scan_id,
                    MIN(evaluated_at) AS scan_time,
                    COUNT(*) AS markets_evaluated,
                    SUM(CASE WHEN risk_gate_approved THEN 1 ELSE 0 END) AS approved_count,
                    MAX(net_edge_pct) AS best_net_edge_pct,
                    AVG(net_edge_pct) AS avg_net_edge_pct
                FROM arbitrage_opportunities
                WHERE executable = TRUE AND net_edge_pct IS NOT NULL
                GROUP BY scan_id
                ORDER BY scan_time ASC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def upsert_cross_venue_pair(self, pair: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cross_venue_pairs VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT (pair_id) DO UPDATE SET
                    poly_market_id = excluded.poly_market_id,
                    poly_question = excluded.poly_question,
                    kalshi_ticker = excluded.kalshi_ticker,
                    kalshi_title = excluded.kalshi_title,
                    resolution_source_verified = excluded.resolution_source_verified,
                    predicate_verified = excluded.predicate_verified,
                    polarity_inverted = excluded.polarity_inverted,
                    void_rules_compatible = excluded.void_rules_compatible,
                    allow_early_close_variance = excluded.allow_early_close_variance,
                    identical_settlement_verified = excluded.identical_settlement_verified,
                    settlement_status = excluded.settlement_status,
                    mismatch_reasons_json = excluded.mismatch_reasons_json,
                    cutoff_delta_hours = excluded.cutoff_delta_hours,
                    notes = excluded.notes
                """,
                [
                    pair["pair_id"],
                    pair["poly_market_id"],
                    pair["poly_question"],
                    pair["kalshi_ticker"],
                    pair["kalshi_title"],
                    bool(pair["resolution_source_verified"]),
                    bool(pair["predicate_verified"]),
                    bool(pair["polarity_inverted"]),
                    bool(pair["void_rules_compatible"]),
                    bool(pair["allow_early_close_variance"]),
                    bool(pair["identical_settlement_verified"]),
                    pair["settlement_status"],
                    json.dumps(list(pair.get("mismatch_reasons", []))),
                    pair.get("cutoff_delta_hours"),
                    pair.get("notes", ""),
                    pair["created_at"],
                ],
            )
        return pair

    def list_cross_venue_pairs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cross_venue_pairs ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            out = []
            for r in rows:
                d = dict(zip(cols, r))
                d["mismatch_reasons"] = json.loads(d.pop("mismatch_reasons_json", "[]"))
                out.append(d)
            return out

    def get_cross_venue_pair(self, pair_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cross_venue_pairs WHERE pair_id = ?", [pair_id]
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self._conn.description]
            d = dict(zip(cols, row))
            d["mismatch_reasons"] = json.loads(d.pop("mismatch_reasons_json", "[]"))
            return d

    def record_cross_venue_opportunity(self, opp: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cross_venue_opportunities VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    opp["xopp_id"],
                    opp["scan_id"],
                    opp["pair_id"],
                    opp["poly_market_id"],
                    opp["kalshi_ticker"],
                    opp["question"],
                    opp["direction"],
                    opp["leg1_venue"],
                    opp["leg1_outcome"],
                    opp["leg2_venue"],
                    opp["leg2_outcome"],
                    float(opp["target_shares"]),
                    opp.get("leg1_vwap"),
                    opp.get("leg2_vwap"),
                    opp.get("combined_raw_cost"),
                    float(opp["total_fees_usd"]),
                    float(opp["slippage_buffer_usd"]),
                    opp.get("total_executable_cost"),
                    opp.get("gross_edge_pct"),
                    opp.get("net_edge_pct"),
                    float(opp["liquidity_participation_pct"]),
                    float(opp["leg_imbalance_ratio"]),
                    bool(opp["executable"]),
                    bool(opp["identical_settlement_verified"]),
                    opp["settlement_status"],
                    bool(opp["risk_gate_approved"]),
                    opp["risk_gate_reason"],
                    opp["risk_audit_id"],
                    int(opp["data_age_ms"]),
                    opp.get("route_a_net_edge_pct"),
                    opp.get("route_b_net_edge_pct"),
                    opp.get("provenance", "LIVE"),
                    opp["evaluated_at"],
                ],
            )

    def list_cross_venue_opportunities(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cross_venue_opportunities ORDER BY evaluated_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def record_paper_order(self, order: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO paper_orders VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    order["order_id"],
                    order["execution_id"],
                    order["pair_id"],
                    order["audit_id"],
                    int(order["leg_number"]),
                    order["venue"],
                    order["market_ref"],
                    order["side"],
                    order["outcome"],
                    order["order_type"],
                    float(order["requested_shares"]),
                    float(order["filled_shares"]),
                    float(order["limit_price"]),
                    order.get("avg_fill_price"),
                    float(order["fees_usd"]),
                    int(order["latency_ms"]),
                    order["status"],
                    order["reason"],
                    "SIMULATED",
                    order["created_at"],
                ],
            )

    def record_paper_fill(self, fill: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO paper_fills VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    fill["fill_id"],
                    fill["execution_id"],
                    fill["order_id"],
                    fill["venue"],
                    fill["market_ref"],
                    fill["side"],
                    fill["outcome"],
                    float(fill["shares"]),
                    float(fill["fill_price"]),
                    float(fill["notional_usd"]),
                    float(fill["fee_usd"]),
                    bool(fill["is_unwind"]),
                    "SIMULATED",
                    fill["filled_at"],
                ],
            )

    def record_paper_position(self, pos: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO paper_positions VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    pos["position_id"],
                    pos["execution_id"],
                    pos["pair_id"],
                    pos["venue"],
                    pos["market_ref"],
                    pos["outcome"],
                    float(pos["shares"]),
                    float(pos["entry_price"]),
                    float(pos["notional_usd"]),
                    float(pos["locked_payout_usd"]),
                    pos["status"],
                    "SIMULATED",
                    pos["updated_at"],
                ],
            )

    def record_paper_pnl(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO paper_pnl_ledger VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    entry["ledger_id"],
                    entry["execution_id"],
                    entry["pair_id"],
                    entry["event_type"],
                    entry["outcome_status"],
                    float(entry["matched_shares"]),
                    float(entry["unwound_shares"]),
                    float(entry["gross_cost_usd"]),
                    float(entry["fees_usd"]),
                    float(entry["realized_pnl_usd"]),
                    float(entry["locked_arb_edge_usd"]),
                    entry["notes"],
                    "SIMULATED",
                    entry["recorded_at"],
                ],
            )

    def list_paper_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_orders ORDER BY created_at DESC LIMIT ?", [limit]
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def list_paper_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_fills ORDER BY filled_at DESC LIMIT ?", [limit]
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def list_paper_positions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_positions ORDER BY updated_at DESC LIMIT ?", [limit]
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def list_paper_pnl(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM paper_pnl_ledger ORDER BY recorded_at DESC LIMIT ?", [limit]
            ).fetchall()
            cols = [d[0] for d in self._conn.description]
            return [dict(zip(cols, r)) for r in rows]

    def get_paper_venue_exposures(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT LOWER(venue) AS v, COALESCE(SUM(notional_usd), 0.0) AS exp
                FROM paper_positions
                WHERE status IN ('OPEN_HEDGED', 'OPEN_UNHEDGED')
                GROUP BY LOWER(venue)
                """
            ).fetchall()
            exposures = {"polymarket": 0.0, "kalshi": 0.0}
            for venue_name, exp in rows:
                exposures[str(venue_name)] = round(float(exp or 0.0), 4)
            return exposures

    def get_daily_tier1_simulated_pnl_usd(self) -> float:
        with self._lock:
            today_prefix = datetime.now(timezone.utc).date().isoformat()
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl_usd), 0.0)
                FROM paper_pnl_ledger
                WHERE SUBSTR(recorded_at, 1, 10) = ?
                """,
                [today_prefix],
            ).fetchone()
            return round(float(row[0] or 0.0), 4) if row else 0.0

    def get_daily_tier1_simulated_loss_usd(self) -> float:
        """Return positive loss magnitude today (>= 0.0) for Risk Gate daily loss limit enforcement."""
        pnl = self.get_daily_tier1_simulated_pnl_usd()
        return round(max(0.0, -pnl), 4)

    def get_paper_summary(self) -> dict[str, Any]:
        with self._lock:
            exposures = self.get_paper_venue_exposures()
            daily_realized = self.get_daily_tier1_simulated_pnl_usd()
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS executions,
                    COALESCE(SUM(realized_pnl_usd), 0.0) AS total_realized_pnl_usd,
                    COALESCE(SUM(locked_arb_edge_usd), 0.0) AS total_locked_arb_edge_usd,
                    COALESCE(SUM(fees_usd), 0.0) AS total_fees_usd,
                    COALESCE(SUM(matched_shares), 0.0) AS total_matched_shares,
                    COALESCE(SUM(unwound_shares), 0.0) AS total_unwound_shares
                FROM paper_pnl_ledger
                """
            ).fetchone()
            return {
                "provenance": "SIMULATED",
                "venue_exposures": exposures,
                "deployed_exposure_usd": round(sum(exposures.values()), 4),
                "daily_simulated_pnl_usd": daily_realized,
                "daily_simulated_loss_usd": round(max(0.0, -daily_realized), 4),
                "executions": int(row[0] or 0),
                "total_realized_pnl_usd": round(float(row[1] or 0.0), 4),
                "total_locked_arb_edge_usd": round(float(row[2] or 0.0), 4),
                "total_fees_usd": round(float(row[3] or 0.0), 4),
                "total_matched_shares": round(float(row[4] or 0.0), 4),
                "total_unwound_shares": round(float(row[5] or 0.0), 4),
            }

    def get_counts(self) -> dict[str, int]:
        with self._lock:
            snaps = self._conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
            opps = self._conn.execute("SELECT COUNT(*) FROM arbitrage_opportunities").fetchone()[0]
            audits = self._conn.execute("SELECT COUNT(*) FROM risk_gate_audit").fetchone()[0]
            memories = self._conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
            xpairs = self._conn.execute("SELECT COUNT(*) FROM cross_venue_pairs").fetchone()[0]
            xopps = self._conn.execute("SELECT COUNT(*) FROM cross_venue_opportunities").fetchone()[0]
            porders = self._conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0]
            pfills = self._conn.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
            smem = self._conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
            lcycles = self._conn.execute("SELECT COUNT(*) FROM learning_cycles").fetchone()[0]
            regcases = self._conn.execute("SELECT COUNT(*) FROM generated_regression_cases").fetchone()[0]
            atasks = self._conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
            workers = self._conn.execute(
                "SELECT COUNT(*) FROM worker_registry WHERE status != 'STOPPED'"
            ).fetchone()[0]
            blockers = self._conn.execute("SELECT COUNT(*) FROM blocker_events").fetchone()[0]
            return {
                "snapshots": int(snaps),
                "opportunities": int(opps),
                "audits": int(audits),
                "memories": int(memories),
                "cross_venue_pairs": int(xpairs),
                "cross_venue_opportunities": int(xopps),
                "paper_orders": int(porders),
                "paper_fills": int(pfills),
                "structured_memories": int(smem),
                "learning_cycles": int(lcycles),
                "regression_cases": int(regcases),
                "agent_tasks": int(atasks),
                "active_workers": int(workers),
                "blocker_events": int(blockers),
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
