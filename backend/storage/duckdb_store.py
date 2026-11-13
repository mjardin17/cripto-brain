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

    def get_counts(self) -> dict[str, int]:
        with self._lock:
            snaps = self._conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
            opps = self._conn.execute("SELECT COUNT(*) FROM arbitrage_opportunities").fetchone()[0]
            audits = self._conn.execute("SELECT COUNT(*) FROM risk_gate_audit").fetchone()[0]
            memories = self._conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
            return {
                "snapshots": int(snaps),
                "opportunities": int(opps),
                "audits": int(audits),
                "memories": int(memories),
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
