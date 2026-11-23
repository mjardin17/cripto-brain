"""Stage 3D Probability Intelligence, Calibration Engine, Agent Scorecards & Config Governance.

Invariants:
- Only RESOLVED predictions with confirmed outcome (0 or 1) count toward Brier, log loss, ECE, and scorecards.
- VOIDED and OPEN predictions never pollute calibration statistics.
- Comparing/ranking agents requires `resolved_count >= min_resolved_samples` (default: 10).
- Prediction errors deterministically log into the Stage 3B LearningEngine.
- Configuration proposals are strictly forbidden on `RISK_GATE` or `SECURITY_PERMISSIONS`,
  and activation requires independent review + passing regression test (`status == 'PASSING'`).
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import normalize_evidence_list
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

FORBIDDEN_CONFIG_DOMAINS = {"RISK_GATE", "RISK_POLICY", "SECURITY_PERMISSIONS", "LIVE_TRADING"}
ALLOWED_CONFIG_DOMAINS = {"AGENT_PIPELINE", "MODEL_ROUTING", "CALIBRATION_PARAMS"}
ALLOWED_ARENA_DIRECTIONS = {"YES", "NO", "UP", "DOWN"}
ALLOWED_DATASET_SPLITS = {"TRAIN", "VALIDATION", "TEST"}
SPLIT_ORDER = {"TRAIN": 0, "VALIDATION": 1, "TEST": 2}
_HASH_PATTERN = re.compile(r"^[A-Fa-f0-9]{16,128}$|^sha256:[A-Fa-f0-9]{16,128}$")


def parse_iso_utc(raw: str, field_name: str = "timestamp") -> datetime:
    cleaned = (raw or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    iso_candidate = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    try:
        dt = datetime.fromisoformat(iso_candidate)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 {field_name}: '{cleaned}'") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_and_validate_direction(predicted_prob: float, predicted_direction: str) -> str:
    norm_dir = (predicted_direction or "").strip().upper()
    if norm_dir not in ALLOWED_ARENA_DIRECTIONS:
        raise ValueError(
            f"predicted_direction must be one of {sorted(ALLOWED_ARENA_DIRECTIONS)}, got '{predicted_direction}'"
        )
    if norm_dir in ("YES", "UP") and predicted_prob < 0.5:
        raise ValueError(
            f"predicted_direction '{norm_dir}' contradicts predicted_prob={predicted_prob:.4f} (< 0.5)"
        )
    if norm_dir in ("NO", "DOWN") and predicted_prob > 0.5:
        raise ValueError(
            f"predicted_direction '{norm_dir}' contradicts predicted_prob={predicted_prob:.4f} (> 0.5)"
        )
    return norm_dir


def compute_brier_score(predicted_prob: float, actual_outcome: int) -> float:
    raw_p = float(predicted_prob)
    if not math.isfinite(raw_p):
        raise ValueError("predicted_prob must be finite")
    p = min(1.0, max(0.0, raw_p))
    y = 1 if int(actual_outcome) == 1 else 0
    return round((p - y) ** 2, 6)


def compute_log_loss(predicted_prob: float, actual_outcome: int, eps: float = 1e-6) -> float:
    raw_p = float(predicted_prob)
    if not math.isfinite(raw_p):
        raise ValueError("predicted_prob must be finite")
    p = min(1.0 - eps, max(eps, raw_p))
    y = 1 if int(actual_outcome) == 1 else 0
    loss = -(y * math.log(p) + (1 - y) * math.log(1.0 - p))
    return round(loss, 6)


class ProbabilityIntelligenceEngine:
    """Tracks advisory probability estimates, confirmed resolutions, calibration buckets, and agent scorecards."""

    DEFAULT_MIN_SAMPLES_FOR_COMPARISON = 10

    def __init__(self, store: DuckDBStore, learning: LearningEngine) -> None:
        self.store = store
        self.learning = learning

    def _row_to_prediction(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["evidence"] = json.loads(d.pop("evidence_json", "[]"))
        d["source_system"] = str(d.get("source_system") or "INTERNAL").upper()
        d["task_category"] = str(d.get("task_category") or d.get("market_category") or "general")
        return d

    def get_prediction(self, prediction_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM predictions WHERE prediction_id = ?",
                [prediction_id],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_prediction(cols, row)

    def list_predictions(
        self,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status.upper())
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM predictions {where_sql} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_prediction(cols, r) for r in rows]

    def record_prediction(
        self,
        *,
        agent_id: str,
        market_id: str,
        venue: str,
        subject_key: str,
        question: str,
        predicted_prob: float,
        market_implied_prob: float | None = None,
        confidence: float = 0.70,
        rationale: str = "",
        evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
        provenance: str = "SIMULATED",
        market_category: str = "general",
    ) -> dict[str, Any]:
        raw_p = float(predicted_prob)
        raw_conf = float(confidence)
        if not math.isfinite(raw_p) or not math.isfinite(raw_conf):
            raise ValueError("predicted_prob and confidence must be finite numbers")
        if market_implied_prob is not None and not math.isfinite(float(market_implied_prob)):
            raise ValueError("market_implied_prob must be a finite number")
        p = round(min(0.9999, max(0.0001, raw_p)), 6)
        conf = round(min(1.0, max(0.0, raw_conf)), 4)
        imp = (
            round(min(0.9999, max(0.0001, float(market_implied_prob))), 6)
            if market_implied_prob is not None
            else None
        )
        ev_list = normalize_evidence_list(evidence)
        pred_id = f"PRED-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()
        cat = (market_category or "general").strip().lower()[:64] or "general"

        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO predictions (
                    prediction_id, agent_id, market_id, venue, subject_key, question,
                    predicted_prob, market_implied_prob, confidence, rationale, evidence_json,
                    provenance, status, actual_outcome, brier_score, log_loss, learning_cycle_id,
                    created_at, resolved_at, market_category
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    pred_id,
                    agent_id.strip(),
                    market_id.strip(),
                    venue.strip().lower(),
                    subject_key.strip().lower(),
                    question.strip(),
                    p,
                    imp,
                    conf,
                    rationale.strip(),
                    json.dumps(ev_list),
                    provenance.upper(),
                    "OPEN",
                    None,
                    None,
                    None,
                    None,
                    now_iso,
                    None,
                    cat,
                ],
            )
            created = self.get_prediction(pred_id)
            assert created is not None
            return created

    def resolve_prediction(
        self,
        prediction_id: str,
        *,
        outcome: int | str,
        resolution_source: str = "confirmed_settlement",
        reviewer_id: str = "calibration_auditor",
        notes: str = "",
        provenance: str = "OPERATOR_CONFIRMED",
        create_error_learning: bool = True,
        external_learning_cycle_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an OPEN prediction cleanly; voided predictions never receive binary scores or mistake training."""
        with self.store._lock:
            pred = self.get_prediction(prediction_id)
            if not pred:
                raise ValueError(f"Unknown prediction_id '{prediction_id}'")
            if pred["status"] != "OPEN":
                raise ValueError(f"Prediction '{prediction_id}' already finalized with status '{pred['status']}'")

            now_iso = utc_now_iso()
            notes_clean = (notes or "").strip()[:600]
            source_clean = (resolution_source or "confirmed_settlement").strip()[:160]
            prov_clean = (provenance or "OPERATOR_CONFIRMED").strip().upper()[:64]
            reviewer_clean = (reviewer_id or "calibration_auditor").strip()[:64]

            if isinstance(outcome, str) and outcome.strip().upper() == "VOID":
                merged_void_ev = list(pred["evidence"]) + [
                    {
                        "source_type": "CONFIRMED_VOID_RESOLUTION",
                        "source_ref_id": source_clean,
                        "resolution_result": "VOID",
                        "actual_outcome": None,
                        "brier_score": None,
                        "log_loss": None,
                        "scored": False,
                        "confirmed_by": reviewer_clean,
                        "resolved_at": now_iso,
                        "provenance": prov_clean,
                        "notes": notes_clean,
                    }
                ]
                self.store._conn.execute(
                    """
                    UPDATE predictions SET
                        status = 'VOIDED',
                        actual_outcome = NULL,
                        brier_score = NULL,
                        log_loss = NULL,
                        evidence_json = ?,
                        learning_cycle_id = ?,
                        resolved_at = ?
                    WHERE prediction_id = ?
                    """,
                    [json.dumps(merged_void_ev), external_learning_cycle_id, now_iso, prediction_id],
                )
                updated = self.get_prediction(prediction_id)
                assert updated is not None
                return updated

            if outcome not in (0, 1, "0", "1"):
                raise ValueError("Resolved outcome must be 0, 1, or 'VOID'")

            y = int(outcome)
            brier = compute_brier_score(pred["predicted_prob"], y)
            ll = compute_log_loss(pred["predicted_prob"], y)

            # Link prediction outcomes to Stage 3B Learning Core (especially errors / high Brier)
            # unless an outer atomic resolution flow supplies its unified learning cycle ID
            # OR the prediction belongs to a quarantined TEST split (TEST-set contamination prevention).
            is_test_split = str(pred.get("dataset_split") or "").strip().upper() == "TEST"
            direction_wrong = (pred["predicted_prob"] >= 0.5 and y == 0) or (
                pred["predicted_prob"] < 0.5 and y == 1
            )
            cycle_id: str | None = external_learning_cycle_id
            if (
                cycle_id is None
                and create_error_learning
                and not is_test_split
                and (brier > 0.15 or direction_wrong)
            ):
                eff_reviewer = (
                    reviewer_clean
                    if reviewer_clean.lower() != pred["agent_id"].strip().lower()
                    else "calibration_auditor"
                )
                cyc = self.learning.record_prediction_error_learning(
                    prediction_id=prediction_id,
                    agent_id=pred["agent_id"],
                    subject_key=pred["subject_key"],
                    predicted_prob=pred["predicted_prob"],
                    actual_outcome=y,
                    reviewer_id=eff_reviewer,
                )
                cycle_id = cyc["cycle_id"]

            merged_ev = list(pred["evidence"]) + [
                {
                    "source_type": "CONFIRMED_RESOLUTION",
                    "source_ref_id": source_clean,
                    "outcome_definition": "FORECAST_YES_PROPOSITION",
                    "resolution_result": "YES" if y == 1 else "NO",
                    "actual_outcome": y,
                    "brier_score": brier,
                    "log_loss": ll,
                    "scored": True,
                    "confirmed_by": reviewer_clean,
                    "resolved_at": now_iso,
                    "provenance": prov_clean,
                    "notes": notes_clean,
                }
            ]
            self.store._conn.execute(
                """
                UPDATE predictions SET
                    status = 'RESOLVED',
                    actual_outcome = ?,
                    brier_score = ?,
                    log_loss = ?,
                    evidence_json = ?,
                    learning_cycle_id = ?,
                    resolved_at = ?
                WHERE prediction_id = ?
                """,
                [y, brier, ll, json.dumps(merged_ev), cycle_id, now_iso, prediction_id],
            )
            updated = self.get_prediction(prediction_id)
            assert updated is not None
            return updated

    # ------------------------------------------------------------------
    # Dataset Split Registry & Leakage Prevention (TRAIN / VALIDATION / TEST)
    # ------------------------------------------------------------------
    def register_dataset_snapshot(
        self,
        *,
        dataset_snapshot_id: str,
        dataset_version: str,
        snapshot_hash: str,
        split: str,
        window_start: str,
        window_end: str,
        market_scope: str = "prediction_market_binary",
        description: str = "",
        created_by: str = "system",
    ) -> dict[str, Any]:
        """Register an immutable TRAIN/VALIDATION/TEST dataset snapshot and block split overlap."""
        snap_id = (dataset_snapshot_id or "").strip()[:128]
        ds_ver = (dataset_version or "").strip()[:80]
        snap_hash = (snapshot_hash or "").strip()[:140]
        norm_split = (split or "").strip().upper()
        norm_scope = (market_scope or "prediction_market_binary").strip().lower()[:64] or "prediction_market_binary"

        if not snap_id or not ds_ver or not snap_hash:
            raise ValueError("dataset_snapshot_id, dataset_version, and snapshot_hash are required")
        if not _HASH_PATTERN.match(snap_hash):
            raise ValueError(
                "snapshot_hash must be a valid hex digest (16-128 hex chars, optional 'sha256:' prefix)"
            )
        if norm_split not in ALLOWED_DATASET_SPLITS:
            raise ValueError(f"split must be one of {sorted(ALLOWED_DATASET_SPLITS)}, got '{split}'")

        start_dt = parse_iso_utc(window_start, field_name="window_start")
        end_dt = parse_iso_utc(window_end, field_name="window_end")
        if end_dt <= start_dt:
            raise ValueError("Dataset snapshot window_end must be strictly after window_start")

        with self.store._lock:
            existing = self.store.get_dataset_snapshot(snap_id)
            if existing is not None:
                same_meta = (
                    existing["dataset_version"] == ds_ver
                    and existing["snapshot_hash"] == snap_hash
                    and existing["split"] == norm_split
                    and parse_iso_utc(existing["window_start"]) == start_dt
                    and parse_iso_utc(existing["window_end"]) == end_dt
                    and existing["market_scope"] == norm_scope
                )
                if same_meta:
                    return {**existing, "deduplicated": True}
                raise ValueError(
                    f"Dataset snapshot '{snap_id}' already exists with conflicting metadata/hash/split"
                )

            siblings = self.store.list_dataset_snapshots(
                dataset_version=ds_ver,
                market_scope=norm_scope,
                limit=500,
            )
            for sib in siblings:
                sib_split = str(sib["split"]).upper()
                sib_start = parse_iso_utc(sib["window_start"])
                sib_end = parse_iso_utc(sib["window_end"])
                if sib_split != norm_split:
                    # Strictly forbid overlapping time windows across TRAIN / VALIDATION / TEST
                    if start_dt < sib_end and sib_start < end_dt:
                        raise ValueError(
                            f"Overlapping time window detected between {norm_split} "
                            f"[{start_dt.isoformat()}, {end_dt.isoformat()}] and existing {sib_split} "
                            f"snapshot '{sib['dataset_snapshot_id']}' "
                            f"[{sib_start.isoformat()}, {sib_end.isoformat()}]"
                        )
                    # Enforce chronological partition order: TRAIN <= VALIDATION <= TEST
                    if SPLIT_ORDER[norm_split] > SPLIT_ORDER[sib_split] and start_dt < sib_end:
                        raise ValueError(
                            f"Split chronological violation: {norm_split} window_start ({start_dt.isoformat()}) "
                            f"must not precede {sib_split} window_end ({sib_end.isoformat()})"
                        )
                    if SPLIT_ORDER[norm_split] < SPLIT_ORDER[sib_split] and end_dt > sib_start:
                        raise ValueError(
                            f"Split chronological violation: {norm_split} window_end ({end_dt.isoformat()}) "
                            f"must not exceed {sib_split} window_start ({sib_start.isoformat()})"
                        )

            created = self.store.insert_dataset_snapshot(
                {
                    "dataset_snapshot_id": snap_id,
                    "dataset_version": ds_ver,
                    "snapshot_hash": snap_hash,
                    "split": norm_split,
                    "window_start": start_dt.isoformat(),
                    "window_end": end_dt.isoformat(),
                    "market_scope": norm_scope,
                    "description": (description or "").strip()[:400],
                    "created_by": (created_by or "system").strip()[:64],
                    "created_at": utc_now_iso(),
                }
            )
            return {**created, "deduplicated": False}

    def list_dataset_snapshots(
        self,
        *,
        dataset_version: str | None = None,
        split: str | None = None,
        market_scope: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.store.list_dataset_snapshots(
            dataset_version=dataset_version,
            split=split,
            market_scope=market_scope,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Arena-to-Cripto-Brain Forecast Ingestion (Unified Predictions Store)
    # ------------------------------------------------------------------
    def get_prediction_by_external_id(self, external_forecast_id: str) -> dict[str, Any] | None:
        cleaned = (external_forecast_id or "").strip()
        if not cleaned:
            return None
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM predictions WHERE external_forecast_id = ? LIMIT 1",
                [cleaned],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_prediction(cols, row)

    def ingest_arena_forecast(
        self,
        *,
        model_id: str,
        provider: str,
        task_category: str,
        prompt_version: str,
        benchmark_version: str,
        market_id: str,
        venue: str,
        timeframe: str,
        forecast_timestamp: str,
        dataset_snapshot_id: str,
        predicted_prob: float,
        predicted_direction: str,
        dataset_version: str | None = None,
        snapshot_hash: str | None = None,
        dataset_split: str | None = None,
        arena_routing_score: float | None = None,
        benchmark_run_id: str | None = None,
        require_registered_snapshot: bool = False,
        enforce_freshness: bool = False,
        max_snapshot_lag_hours: float = 168.0,
        max_live_forecast_age_hours: float = 72.0,
        subject_key: str | None = None,
        question: str = "",
        market_implied_prob: float | None = None,
        confidence: float = 0.70,
        rationale: str = "",
        evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
        provenance: str = "LIVE",
        external_forecast_id: str | None = None,
        ingested_by: str = "arena_ingestor",
    ) -> dict[str, Any]:
        """Ingest a structured AI Orchestra Arena forecast into the unified predictions table.

        Enforces dataset split registration consistency, future-data leakage checks,
        post-cutoff target leakage checks, and stale forecast rejection.
        """
        norm_model = model_id.strip()
        norm_provider = provider.strip().lower()
        norm_task_cat = task_category.strip().lower()
        norm_prompt_ver = prompt_version.strip()
        norm_bench_ver = benchmark_version.strip()
        norm_market = market_id.strip()
        norm_venue = venue.strip().lower()
        norm_timeframe = timeframe.strip()
        norm_snapshot = dataset_snapshot_id.strip()

        for field_label, val in (
            ("model_id", norm_model),
            ("provider", norm_provider),
            ("task_category", norm_task_cat),
            ("prompt_version", norm_prompt_ver),
            ("benchmark_version", norm_bench_ver),
            ("market_id", norm_market),
            ("venue", norm_venue),
            ("timeframe", norm_timeframe),
            ("dataset_snapshot_id", norm_snapshot),
        ):
            if not val:
                raise ValueError(f"{field_label} is required for Arena forecast ingestion")

        raw_p = float(predicted_prob)
        raw_conf = float(confidence)
        if not math.isfinite(raw_p) or raw_p < 0.0 or raw_p > 1.0:
            raise ValueError("predicted_prob must be a finite probability in [0.0, 1.0]")
        if not math.isfinite(raw_conf) or raw_conf < 0.0 or raw_conf > 1.0:
            raise ValueError("confidence must be a finite number in [0.0, 1.0]")
        if market_implied_prob is not None:
            raw_imp = float(market_implied_prob)
            if not math.isfinite(raw_imp) or raw_imp < 0.0 or raw_imp > 1.0:
                raise ValueError("market_implied_prob must be a finite probability in [0.0, 1.0]")

        norm_routing_score: float | None = None
        if arena_routing_score is not None:
            raw_rs = float(arena_routing_score)
            if not math.isfinite(raw_rs) or raw_rs < 0.0 or raw_rs > 1.0:
                raise ValueError("arena_routing_score must be a finite number in [0.0, 1.0]")
            norm_routing_score = round(raw_rs, 6)

        norm_direction = normalize_and_validate_direction(raw_p, predicted_direction)
        parsed_ts = parse_iso_utc(forecast_timestamp, field_name="forecast_timestamp")
        norm_forecast_ts = parsed_ts.isoformat()

        parsed_tf_dt: datetime | None = None
        try:
            parsed_tf_dt = parse_iso_utc(norm_timeframe, field_name="timeframe")
        except ValueError:
            parsed_tf_dt = None

        if parsed_tf_dt is not None and parsed_ts > parsed_tf_dt:
            raise ValueError(
                f"Post-cutoff forecast rejected: forecast_timestamp ({norm_forecast_ts}) "
                f"is after market timeframe cutoff ({parsed_tf_dt.isoformat()})"
            )

        now_dt = datetime.now(timezone.utc)
        norm_prov = provenance.strip().upper() or "LIVE"
        if enforce_freshness and (now_dt - parsed_ts) > timedelta(hours=max(0.01, float(max_live_forecast_age_hours))):
            raise ValueError(
                f"Stale forecast rejected: forecast_timestamp ({norm_forecast_ts}) exceeds "
                f"max age of {max_live_forecast_age_hours:.1f}h"
            )

        req_ds_ver = (dataset_version or "").strip() or None
        req_snap_hash = (snapshot_hash or "").strip() or None
        req_split = (dataset_split or "").strip().upper() or None
        req_run_id = (benchmark_run_id or "").strip()[:120] or None

        if req_split is not None and req_split not in ALLOWED_DATASET_SPLITS:
            raise ValueError(
                f"dataset_split must be one of {sorted(ALLOWED_DATASET_SPLITS)}, got '{dataset_split}'"
            )
        if req_snap_hash is not None and not _HASH_PATTERN.match(req_snap_hash):
            raise ValueError(
                "snapshot_hash must be a valid hex digest (16-128 hex chars, optional 'sha256:' prefix)"
            )

        ext_id = (external_forecast_id or "").strip() or None

        with self.store._lock:
            # Validate registered dataset snapshot split, hash, and leakage bounds BEFORE dedup/insert
            reg_snap = self.store.get_dataset_snapshot(norm_snapshot)
            if reg_snap is None and require_registered_snapshot:
                raise ValueError(
                    f"Unknown dataset_snapshot_id '{norm_snapshot}': register dataset snapshot "
                    "with explicit TRAIN/VALIDATION/TEST split before Arena sync"
                )

            bound_ds_ver = req_ds_ver
            bound_snap_hash = req_snap_hash
            bound_split = req_split
            bound_win_start: str | None = None
            bound_win_end: str | None = None

            if reg_snap is not None:
                if req_ds_ver and req_ds_ver != reg_snap["dataset_version"]:
                    raise ValueError(
                        f"Dataset snapshot mismatch for '{norm_snapshot}': dataset_version "
                        f"'{req_ds_ver}' does not match registered '{reg_snap['dataset_version']}'"
                    )
                if req_snap_hash and req_snap_hash != reg_snap["snapshot_hash"]:
                    raise ValueError(
                        f"Dataset snapshot mismatch for '{norm_snapshot}': snapshot_hash "
                        f"does not match registered digest"
                    )
                if req_split and req_split != reg_snap["split"]:
                    raise ValueError(
                        f"Dataset snapshot mismatch for '{norm_snapshot}': split '{req_split}' "
                        f"does not match registered '{reg_snap['split']}'"
                    )
                bound_ds_ver = reg_snap["dataset_version"]
                bound_snap_hash = reg_snap["snapshot_hash"]
                bound_split = reg_snap["split"]
                bound_win_start = reg_snap["window_start"]
                bound_win_end = reg_snap["window_end"]

                snap_end_dt = parse_iso_utc(bound_win_end, field_name="dataset_window_end")
                # Future-data leakage: dataset snapshot cannot contain observations after forecast_timestamp
                if snap_end_dt > parsed_ts:
                    raise ValueError(
                        f"Future-data leakage detected: dataset snapshot '{norm_snapshot}' "
                        f"window_end ({bound_win_end}) is after forecast_timestamp ({norm_forecast_ts})"
                    )
                # Target leakage: market cutoff cannot occur on or before the dataset feature window closes
                if parsed_tf_dt is not None and parsed_tf_dt <= snap_end_dt:
                    raise ValueError(
                        f"Target leakage detected: market timeframe ({parsed_tf_dt.isoformat()}) "
                        f"falls on or before dataset snapshot window_end ({bound_win_end})"
                    )
                # Stale forecast relative to dataset snapshot window
                lag = parsed_ts - snap_end_dt
                if lag > timedelta(hours=max(0.01, float(max_snapshot_lag_hours))):
                    raise ValueError(
                        f"Stale forecast rejected: forecast_timestamp ({norm_forecast_ts}) lags "
                        f"dataset snapshot window_end ({bound_win_end}) by {lag.total_seconds() / 3600.0:.1f}h "
                        f"(max {max_snapshot_lag_hours:.1f}h)"
                    )

            if ext_id:
                existing = self.get_prediction_by_external_id(ext_id)
                if existing is not None:
                    return {**existing, "deduplicated": True}

            p = round(min(0.9999, max(0.0001, raw_p)), 6)
            conf = round(min(1.0, max(0.0, raw_conf)), 4)
            imp = (
                round(min(0.9999, max(0.0001, float(market_implied_prob))), 6)
                if market_implied_prob is not None
                else None
            )
            canonical_agent_id = f"arena:{norm_provider}/{norm_model}"
            norm_subject = (
                (subject_key or "").strip().lower()
                or f"{norm_task_cat}:{norm_market.lower()}:{norm_timeframe.lower()}"
            )
            norm_question = (
                question.strip()
                or f"Arena forecast ({norm_task_cat}) on {norm_market} [{norm_timeframe}]"
            )
            pred_id = f"PRED-{uuid.uuid4().hex[:10].upper()}"
            now_iso = utc_now_iso()

            arena_ev = {
                "source_type": "ARENA_FORECAST_INGESTION",
                "source_system": "AI_ORCHESTRA_ARENA",
                "model_id": norm_model,
                "provider": norm_provider,
                "task_category": norm_task_cat,
                "prompt_version": norm_prompt_ver,
                "benchmark_version": norm_bench_ver,
                "benchmark_run_id": req_run_id,
                "market_id": norm_market,
                "venue": norm_venue,
                "timeframe": norm_timeframe,
                "forecast_timestamp": norm_forecast_ts,
                "dataset_snapshot_id": norm_snapshot,
                "dataset_version": bound_ds_ver,
                "snapshot_hash": bound_snap_hash,
                "dataset_split": bound_split,
                "dataset_window_start": bound_win_start,
                "dataset_window_end": bound_win_end,
                "predicted_prob": p,
                "predicted_direction": norm_direction,
                "arena_routing_score": norm_routing_score,
                "outcome_definition": "FORECAST_YES_PROPOSITION",
                "external_forecast_id": ext_id,
                "ingested_by": ingested_by.strip()[:64],
                "ingested_at": now_iso,
            }
            ev_list = [arena_ev] + normalize_evidence_list(evidence)

            self.store._conn.execute(
                """
                INSERT INTO predictions (
                    prediction_id, agent_id, market_id, venue, subject_key, question,
                    predicted_prob, market_implied_prob, confidence, rationale, evidence_json,
                    provenance, status, actual_outcome, brier_score, log_loss, learning_cycle_id,
                    created_at, resolved_at, market_category,
                    source_system, external_forecast_id, model_id, provider, task_category,
                    prompt_version, benchmark_version, timeframe, forecast_timestamp,
                    dataset_snapshot_id, predicted_direction,
                    dataset_version, snapshot_hash, dataset_split,
                    dataset_window_start, dataset_window_end, arena_routing_score, benchmark_run_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    pred_id,
                    canonical_agent_id,
                    norm_market,
                    norm_venue,
                    norm_subject[:120],
                    norm_question[:400],
                    p,
                    imp,
                    conf,
                    rationale.strip()[:1000],
                    json.dumps(ev_list),
                    norm_prov,
                    "OPEN",
                    None,
                    None,
                    None,
                    None,
                    now_iso,
                    None,
                    norm_task_cat[:64],
                    "AI_ORCHESTRA_ARENA",
                    ext_id,
                    norm_model[:120],
                    norm_provider[:64],
                    norm_task_cat[:64],
                    norm_prompt_ver[:80],
                    norm_bench_ver[:80],
                    norm_timeframe[:80],
                    norm_forecast_ts,
                    norm_snapshot[:128],
                    norm_direction,
                    bound_ds_ver[:80] if bound_ds_ver else None,
                    bound_snap_hash[:140] if bound_snap_hash else None,
                    bound_split,
                    bound_win_start,
                    bound_win_end,
                    norm_routing_score,
                    req_run_id,
                ],
            )
            created = self.get_prediction(pred_id)
            assert created is not None
            return {**created, "deduplicated": False}

    def ingest_arena_forecasts_batch(
        self,
        items: list[dict[str, Any]],
        *,
        ingested_by: str = "arena_ingestor",
        require_registered_snapshot: bool = False,
        enforce_freshness: bool = False,
        max_snapshot_lag_hours: float = 168.0,
        max_live_forecast_age_hours: float = 72.0,
    ) -> dict[str, Any]:
        """Atomically ingest a batch of Arena forecasts; rolls back all items if any validation fails."""
        if not items:
            raise ValueError("Batch must include at least one Arena forecast item")
        if len(items) > 100:
            raise ValueError("Batch size cannot exceed 100 forecasts per request")
        ingested: list[dict[str, Any]] = []
        deduplicated_count = 0
        with self.store.transaction():
            for item in items:
                payload = dict(item)
                payload.setdefault("require_registered_snapshot", require_registered_snapshot)
                payload.setdefault("enforce_freshness", enforce_freshness)
                payload.setdefault("max_snapshot_lag_hours", max_snapshot_lag_hours)
                payload.setdefault("max_live_forecast_age_hours", max_live_forecast_age_hours)
                res = self.ingest_arena_forecast(ingested_by=ingested_by, **payload)
                if res.get("deduplicated"):
                    deduplicated_count += 1
                ingested.append(res)
        return {
            "count": len(ingested),
            "inserted_count": len(ingested) - deduplicated_count,
            "deduplicated_count": deduplicated_count,
            "forecasts": ingested,
        }

    def list_arena_forecasts(
        self,
        *,
        model_id: str | None = None,
        provider: str | None = None,
        task_category: str | None = None,
        benchmark_version: str | None = None,
        dataset_split: str | None = None,
        dataset_snapshot_id: str | None = None,
        status: str | None = None,
        market_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["source_system = 'AI_ORCHESTRA_ARENA'"]
        params: list[Any] = []
        if model_id:
            clauses.append("model_id = ?")
            params.append(model_id.strip())
        if provider:
            clauses.append("provider = ?")
            params.append(provider.strip().lower())
        if task_category:
            clauses.append("task_category = ?")
            params.append(task_category.strip().lower())
        if benchmark_version:
            clauses.append("benchmark_version = ?")
            params.append(benchmark_version.strip())
        if dataset_split:
            clauses.append("dataset_split = ?")
            params.append(dataset_split.strip().upper())
        if dataset_snapshot_id:
            clauses.append("dataset_snapshot_id = ?")
            params.append(dataset_snapshot_id.strip())
        if status:
            clauses.append("status = ?")
            params.append(status.strip().upper())
        if market_id:
            clauses.append("market_id = ?")
            params.append(market_id.strip())
        sql = f"SELECT * FROM predictions WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_prediction(cols, r) for r in rows]

    @staticmethod
    def _compute_group_metrics(resolved_rows: list[tuple[float, int, float | None, float | None]]) -> dict[str, Any]:
        """Compute verified Brier, log loss, 10-bin ECE, and accuracy from (prob, outcome, brier, ll) tuples."""
        n = len(resolved_rows)
        if n == 0:
            return {
                "resolved_count": 0,
                "mean_brier_score": None,
                "mean_log_loss": None,
                "expected_calibration_error": None,
                "accuracy_pct": None,
            }
        b_vals: list[float] = []
        l_vals: list[float] = []
        hits = 0
        buckets: list[list[tuple[float, int]]] = [[] for _ in range(10)]
        for p, y, b_raw, ll_raw in resolved_rows:
            b = float(b_raw) if b_raw is not None else compute_brier_score(p, y)
            ll = float(ll_raw) if ll_raw is not None else compute_log_loss(p, y)
            b_vals.append(b)
            l_vals.append(ll)
            if (p >= 0.5 and y == 1) or (p < 0.5 and y == 0):
                hits += 1
            idx = min(9, max(0, int(p * 10)))
            buckets[idx].append((p, y))
        ece = sum(
            (len(bkt) / n)
            * abs((sum(x[0] for x in bkt) / len(bkt)) - (sum(x[1] for x in bkt) / len(bkt)))
            for bkt in buckets
            if bkt
        )
        return {
            "resolved_count": n,
            "mean_brier_score": round(sum(b_vals) / n, 6),
            "mean_log_loss": round(sum(l_vals) / n, 6),
            "expected_calibration_error": round(ece, 6),
            "accuracy_pct": round((hits / n) * 100.0, 2),
        }

    def get_arena_model_leaderboard(
        self,
        *,
        task_category: str | None = None,
        benchmark_version: str | None = None,
        dataset_split: str | None = None,
        dataset_snapshot_id: str | None = None,
        timeframe: str | None = None,
        min_resolved_samples: int = DEFAULT_MIN_SAMPLES_FOR_COMPARISON,
    ) -> dict[str, Any]:
        """Aggregate verified calibration scores for ingested Arena models (strictly RESOLVED non-VOID).

        Never ranks tiny samples, never mixes benchmark/split/snapshot conditions in rankings,
        and keeps Arena routing scores strictly separate from verified forecast metrics.
        """
        min_n = max(1, int(min_resolved_samples))
        clauses = ["source_system = 'AI_ORCHESTRA_ARENA'"]
        params: list[Any] = []
        if task_category:
            clauses.append("task_category = ?")
            params.append(task_category.strip().lower())
        if benchmark_version:
            clauses.append("benchmark_version = ?")
            params.append(benchmark_version.strip())
        if dataset_split:
            clauses.append("dataset_split = ?")
            params.append(dataset_split.strip().upper())
        if dataset_snapshot_id:
            clauses.append("dataset_snapshot_id = ?")
            params.append(dataset_snapshot_id.strip())
        if timeframe:
            clauses.append("timeframe = ?")
            params.append(timeframe.strip())
        with self.store._lock:
            rows = self.store._conn.execute(
                f"""
                SELECT
                    provider, model_id, task_category, prompt_version, benchmark_version,
                    timeframe, dataset_snapshot_id, dataset_split,
                    status, predicted_prob, actual_outcome, brier_score, log_loss,
                    arena_routing_score
                FROM predictions
                WHERE {' AND '.join(clauses)}
                """,
                params,
            ).fetchall()

        groups: dict[tuple[str, str, str, str, str, str, str, str], list[tuple[Any, ...]]] = {}
        for row in rows:
            key = (
                str(row[0] or "unknown"),
                str(row[1] or "unknown"),
                str(row[2] or "general"),
                str(row[3] or ""),
                str(row[4] or ""),
                str(row[5] or ""),
                str(row[6] or ""),
                str(row[7] or "UNASSIGNED"),
            )
            groups.setdefault(key, []).append(row)

        entries: list[dict[str, Any]] = []
        for (prov, mod, tcat, pver, bver, tf, snap_id, ds_split), items in groups.items():
            open_cnt = sum(1 for r in items if r[8] == "OPEN")
            void_cnt = sum(1 for r in items if r[8] == "VOIDED")
            resolved_tuples = [
                (float(r[9]), int(r[10]), r[11], r[12])
                for r in items
                if r[8] == "RESOLVED" and r[10] in (0, 1)
            ]
            metrics = self._compute_group_metrics(resolved_tuples)
            res_cnt = metrics["resolved_count"]
            routing_scores = [float(r[13]) for r in items if r[13] is not None]
            avg_routing = round(sum(routing_scores) / len(routing_scores), 6) if routing_scores else None
            eligible = res_cnt >= min_n and metrics["mean_brier_score"] is not None
            entries.append(
                {
                    "agent_id": f"arena:{prov}/{mod}",
                    "provider": prov,
                    "model_id": mod,
                    "task_category": tcat,
                    "prompt_version": pver,
                    "benchmark_version": bver,
                    "timeframe": tf,
                    "dataset_snapshot_id": snap_id,
                    "dataset_split": ds_split,
                    "resolved_count": res_cnt,
                    "open_count": open_cnt,
                    "voided_count": void_cnt,
                    "mean_brier_score": metrics["mean_brier_score"],
                    "mean_log_loss": metrics["mean_log_loss"],
                    "expected_calibration_error": metrics["expected_calibration_error"],
                    "accuracy_pct": metrics["accuracy_pct"],
                    "comparison_eligible": eligible,
                    "comparison_status": (
                        "COMPARABLE"
                        if eligible
                        else f"INSUFFICIENT_SAMPLE ({res_cnt}/{min_n} resolved)"
                    ),
                    "arena_routing_metadata": {
                        "avg_arena_routing_score": avg_routing,
                        "routing_sample_count": len(routing_scores),
                        "excluded_from_verified_ranking": True,
                    },
                }
            )
        entries.sort(
            key=lambda e: (
                not e["comparison_eligible"],
                e["mean_brier_score"] if e["mean_brier_score"] is not None else 999.0,
                -e["resolved_count"],
                e["model_id"],
            )
        )
        return {
            "min_resolved_samples_required": min_n,
            "routing_score_excluded_from_ranking": True,
            "models_count": len(entries),
            "models": entries,
        }

    def get_arena_evaluation_report(
        self,
        *,
        task_category: str | None = None,
        benchmark_version: str | None = None,
        dataset_split: str | None = None,
        dataset_snapshot_id: str | None = None,
        timeframe: str | None = None,
        min_resolved_samples: int = DEFAULT_MIN_SAMPLES_FOR_COMPARISON,
    ) -> dict[str, Any]:
        """Full out-of-sample model evaluation report with identical-snapshot comparisons.

        Guarantees:
        - Tracks Brier, log loss, ECE calibration, accuracy, and sample size by model, task_category, timeframe, and split.
        - Compares models head-to-head strictly on IDENTICAL `(dataset_snapshot_id, benchmark_version, task_category, timeframe, dataset_split)` cohorts and matched markets.
        - Never ranks tiny samples (`< min_resolved_samples`) or mixes different benchmark conditions.
        - Keeps Arena routing scores completely segregated from verified forecast performance.
        """
        min_n = max(1, int(min_resolved_samples))
        clauses = ["source_system = 'AI_ORCHESTRA_ARENA'"]
        params: list[Any] = []
        if task_category:
            clauses.append("task_category = ?")
            params.append(task_category.strip().lower())
        if benchmark_version:
            clauses.append("benchmark_version = ?")
            params.append(benchmark_version.strip())
        if dataset_split:
            clauses.append("dataset_split = ?")
            params.append(dataset_split.strip().upper())
        if dataset_snapshot_id:
            clauses.append("dataset_snapshot_id = ?")
            params.append(dataset_snapshot_id.strip())
        if timeframe:
            clauses.append("timeframe = ?")
            params.append(timeframe.strip())

        with self.store._lock:
            rows = self.store._conn.execute(
                f"""
                SELECT
                    prediction_id, provider, model_id, task_category, prompt_version,
                    benchmark_version, timeframe, market_id, dataset_snapshot_id,
                    COALESCE(dataset_split, 'UNASSIGNED') AS dataset_split,
                    status, predicted_prob, actual_outcome, brier_score, log_loss,
                    arena_routing_score
                FROM predictions
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC
                """,
                params,
            ).fetchall()

        leaderboard = self.get_arena_model_leaderboard(
            task_category=task_category,
            benchmark_version=benchmark_version,
            dataset_split=dataset_split,
            dataset_snapshot_id=dataset_snapshot_id,
            timeframe=timeframe,
            min_resolved_samples=min_n,
        )

        by_cat_acc: dict[str, list[tuple[float, int, float | None, float | None]]] = {}
        by_tf_acc: dict[str, list[tuple[float, int, float | None, float | None]]] = {}
        by_split_acc: dict[str, dict[str, Any]] = {}
        # Same-snapshot cohorts: key = (snap_id, bench_ver, task_cat, timeframe, ds_split)
        # value = { model_key -> { market_id -> (p, y, b, ll, routing_score) } }
        cohorts: dict[tuple[str, str, str, str, str], dict[str, dict[str, tuple[float, int, float | None, float | None, float | None]]]] = {}

        for r in rows:
            _, prov, mod, tcat, pver, bver, tf, mkt_id, snap_id, ds_split, st, p_raw, y_raw, b_raw, ll_raw, rs_raw = r
            tcat_s = str(tcat or "general")
            tf_s = str(tf or "unspecified")
            split_s = str(ds_split or "UNASSIGNED")
            split_bucket = by_split_acc.setdefault(
                split_s,
                {"dataset_split": split_s, "resolved": [], "open_count": 0, "voided_count": 0, "tuning_allowed": split_s != "TEST"},
            )
            if st == "OPEN":
                split_bucket["open_count"] += 1
                continue
            if st == "VOIDED":
                split_bucket["voided_count"] += 1
                continue
            if st != "RESOLVED" or y_raw not in (0, 1):
                continue

            tup = (float(p_raw), int(y_raw), b_raw, ll_raw)
            by_cat_acc.setdefault(tcat_s, []).append(tup)
            by_tf_acc.setdefault(tf_s, []).append(tup)
            split_bucket["resolved"].append(tup)

            cohort_key = (str(snap_id or ""), str(bver or ""), tcat_s, tf_s, split_s)
            model_key = f"{prov}/{mod}@{pver}"
            rs_val = float(rs_raw) if rs_raw is not None else None
            cohorts.setdefault(cohort_key, {}).setdefault(model_key, {})[str(mkt_id)] = (
                float(p_raw),
                int(y_raw),
                b_raw,
                ll_raw,
                rs_val,
            )

        by_task_category = [
            {"task_category": k, **self._compute_group_metrics(v)}
            for k, v in sorted(by_cat_acc.items())
        ]
        by_timeframe = [
            {"timeframe": k, **self._compute_group_metrics(v)}
            for k, v in sorted(by_tf_acc.items())
        ]
        by_split = [
            {
                "dataset_split": k,
                "tuning_allowed": v["tuning_allowed"],
                "open_count": v["open_count"],
                "voided_count": v["voided_count"],
                **self._compute_group_metrics(v["resolved"]),
            }
            for k, v in sorted(by_split_acc.items())
        ]

        same_snapshot_comparisons: list[dict[str, Any]] = []
        for (snap_id, bver, tcat_s, tf_s, split_s), model_map in sorted(cohorts.items()):
            model_keys = sorted(model_map.keys())
            if not model_keys:
                continue
            # Intersect resolved markets across all models in this identical-condition cohort
            common_markets = set.intersection(*(set(model_map[mk].keys()) for mk in model_keys))
            matched_n = len(common_markets)
            eligible = len(model_keys) >= 2 and matched_n >= min_n
            ranked_models: list[dict[str, Any]] = []
            for mk in model_keys:
                matched_tuples = [
                    (model_map[mk][m][0], model_map[mk][m][1], model_map[mk][m][2], model_map[mk][m][3])
                    for m in sorted(common_markets)
                ]
                rs_vals = [model_map[mk][m][4] for m in sorted(common_markets) if model_map[mk][m][4] is not None]
                m_metrics = self._compute_group_metrics(matched_tuples)
                ranked_models.append(
                    {
                        "model_key": mk,
                        "verified_forecast_metrics": m_metrics,
                        "arena_routing_metadata": {
                            "avg_arena_routing_score": round(sum(rs_vals) / len(rs_vals), 6) if rs_vals else None,
                            "routing_sample_count": len(rs_vals),
                            "excluded_from_verified_ranking": True,
                        },
                        "rank": None,
                    }
                )
            if eligible:
                ranked_models.sort(
                    key=lambda item: (
                        float(item["verified_forecast_metrics"]["mean_brier_score"]),
                        float(item["verified_forecast_metrics"]["mean_log_loss"]),
                        item["model_key"],
                    )
                )
                for idx, item in enumerate(ranked_models, start=1):
                    item["rank"] = idx

            if len(model_keys) < 2:
                comp_status = "SINGLE_MODEL_COHORT"
            elif not eligible:
                comp_status = f"INSUFFICIENT_MATCHED_SAMPLES ({matched_n}/{min_n} identical-snapshot markets)"
            else:
                comp_status = "COMPARABLE_IDENTICAL_SNAPSHOT"

            same_snapshot_comparisons.append(
                {
                    "dataset_snapshot_id": snap_id,
                    "benchmark_version": bver,
                    "task_category": tcat_s,
                    "timeframe": tf_s,
                    "dataset_split": split_s,
                    "models_in_cohort": len(model_keys),
                    "matched_market_count": matched_n,
                    "min_resolved_samples_required": min_n,
                    "comparison_eligible": eligible,
                    "comparison_status": comp_status,
                    "models": ranked_models,
                }
            )

        return {
            "provenance": "HISTORICAL",
            "live_trading_enabled": False,
            "min_resolved_samples_required": min_n,
            "routing_score_excluded_from_ranking": True,
            "test_split_quarantined_from_tuning": True,
            "leaderboard": leaderboard,
            "by_task_category": by_task_category,
            "by_timeframe": by_timeframe,
            "by_split": by_split,
            "same_snapshot_comparisons": same_snapshot_comparisons,
            "snapshots": self.list_dataset_snapshots(limit=50),
        }

    def compute_calibration_summary(
        self,
        *,
        agent_id: str | None = None,
        num_buckets: int = 10,
    ) -> dict[str, Any]:
        """Compute Brier score, log loss, ECE, and calibration buckets from RESOLVED predictions ONLY."""
        with self.store._lock:
            clauses = ["1=1"]
            params: list[Any] = []
            if agent_id:
                clauses.append("agent_id = ?")
                params.append(agent_id)
            rows = self.store._conn.execute(
                f"SELECT status, predicted_prob, actual_outcome, brier_score, log_loss FROM predictions WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()

        open_count = sum(1 for r in rows if r[0] == "OPEN")
        voided_count = sum(1 for r in rows if r[0] == "VOIDED")
        resolved = [r for r in rows if r[0] == "RESOLVED" and r[2] in (0, 1)]
        resolved_count = len(resolved)

        # Initialize 10 deterministic probability decile buckets
        buckets_acc: list[list[tuple[float, int]]] = [[] for _ in range(num_buckets)]
        correct_dir = 0
        brier_sum = 0.0
        logloss_sum = 0.0

        for _, p_raw, y_raw, brier_raw, ll_raw in resolved:
            p = float(p_raw)
            y = int(y_raw)
            brier = float(brier_raw) if brier_raw is not None else compute_brier_score(p, y)
            ll = float(ll_raw) if ll_raw is not None else compute_log_loss(p, y)
            brier_sum += brier
            logloss_sum += ll
            if (p >= 0.5 and y == 1) or (p < 0.5 and y == 0):
                correct_dir += 1
            idx = min(num_buckets - 1, max(0, int(p * num_buckets)))
            buckets_acc[idx].append((p, y))

        buckets: list[dict[str, Any]] = []
        ece = 0.0
        for i in range(num_buckets):
            low = round(i / num_buckets, 2)
            high = round((i + 1) / num_buckets, 2)
            items = buckets_acc[i]
            n = len(items)
            if n > 0:
                avg_p = sum(x[0] for x in items) / n
                emp_y = sum(x[1] for x in items) / n
                gap = abs(avg_p - emp_y)
                ece += (n / resolved_count) * gap
                buckets.append(
                    {
                        "bucket_index": i,
                        "range_label": f"{int(low * 100)}–{int(high * 100)}%",
                        "low": low,
                        "high": high,
                        "count": n,
                        "avg_predicted_prob": round(avg_p, 4),
                        "empirical_yes_rate": round(emp_y, 4),
                        "calibration_gap": round(gap, 4),
                    }
                )
            else:
                buckets.append(
                    {
                        "bucket_index": i,
                        "range_label": f"{int(low * 100)}–{int(high * 100)}%",
                        "low": low,
                        "high": high,
                        "count": 0,
                        "avg_predicted_prob": None,
                        "empirical_yes_rate": None,
                        "calibration_gap": None,
                    }
                )

        return {
            "resolved_count": resolved_count,
            "open_count": open_count,
            "voided_count": voided_count,
            "mean_brier_score": round(brier_sum / resolved_count, 6) if resolved_count > 0 else None,
            "mean_log_loss": round(logloss_sum / resolved_count, 6) if resolved_count > 0 else None,
            "accuracy_pct": round((correct_dir / resolved_count) * 100.0, 2) if resolved_count > 0 else None,
            "expected_calibration_error": round(ece, 6) if resolved_count > 0 else None,
            "buckets": buckets,
            "note": "Only RESOLVED predictions with confirmed outcomes (0 or 1) contribute to accuracy, Brier, log loss, and calibration.",
        }

    def get_agent_scorecards(
        self, *, min_resolved_samples: int = DEFAULT_MIN_SAMPLES_FOR_COMPARISON
    ) -> dict[str, Any]:
        """Compute per-agent performance scorecards enforcing minimum sample thresholds before comparison."""
        min_n = max(1, int(min_resolved_samples))
        with self.store._lock:
            pred_agents = [
                r[0]
                for r in self.store._conn.execute("SELECT DISTINCT agent_id FROM predictions").fetchall()
            ]
            task_rows = self.store._conn.execute(
                """
                SELECT LOWER(assigned_agent) AS role_key,
                       SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_cnt,
                       SUM(CASE WHEN status = 'ESCALATED' THEN 1 ELSE 0 END) AS escalated_cnt,
                       COUNT(*) AS total_cnt
                FROM agent_tasks
                GROUP BY LOWER(assigned_agent)
                """
            ).fetchall()

        task_stats = {
            f"{str(r[0])}_agent": {
                "tasks_completed": int(r[1] or 0),
                "tasks_escalated": int(r[2] or 0),
                "tasks_total": int(r[3] or 0),
            }
            for r in task_rows
        }
        all_agents = sorted(set(pred_agents) | set(task_stats.keys()))

        scorecards: list[dict[str, Any]] = []
        ranked_pool: list[dict[str, Any]] = []

        for ag in all_agents:
            cal = self.compute_calibration_summary(agent_id=ag)
            t_stat = task_stats.get(ag, {"tasks_completed": 0, "tasks_escalated": 0, "tasks_total": 0})
            eligible = cal["resolved_count"] >= min_n
            card = {
                "agent_id": ag,
                "resolved_predictions": cal["resolved_count"],
                "open_predictions": cal["open_count"],
                "voided_predictions": cal["voided_count"],
                "mean_brier_score": cal["mean_brier_score"],
                "mean_log_loss": cal["mean_log_loss"],
                "accuracy_pct": cal["accuracy_pct"],
                "expected_calibration_error": cal["expected_calibration_error"],
                "comparison_eligible": eligible,
                "comparison_status": "RANKED" if eligible else f"INSUFFICIENT_SAMPLES (< {min_n})",
                "min_resolved_samples_required": min_n,
                "rank": None,
                **t_stat,
            }
            scorecards.append(card)
            if eligible and card["mean_brier_score"] is not None:
                ranked_pool.append(card)

        ranked_pool.sort(key=lambda c: (float(c["mean_brier_score"]), float(c["mean_log_loss"] or 999.0)))
        for idx, card in enumerate(ranked_pool, start=1):
            card["rank"] = idx

        return {
            "min_resolved_samples_required": min_n,
            "comparable_agents_count": len(ranked_pool),
            "scorecards": scorecards,
        }

    def compute_historical_validation(
        self,
        *,
        agent_id: str | None = None,
        market_category: str | None = None,
        min_resolved_samples: int = DEFAULT_MIN_SAMPLES_FOR_COMPARISON,
    ) -> dict[str, Any]:
        """Historical accuracy backtesting strictly on RESOLVED (0/1) predictions with 95% CIs & baselines."""
        min_n = max(1, int(min_resolved_samples))
        clauses = ["1=1"]
        params: list[Any] = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id.strip())
        if market_category:
            clauses.append("LOWER(COALESCE(market_category, 'general')) = ?")
            params.append(market_category.strip().lower())

        with self.store._lock:
            rows = self.store._conn.execute(
                f"""
                SELECT prediction_id, agent_id, COALESCE(market_category, 'general') AS market_category,
                       status, predicted_prob, market_implied_prob, actual_outcome,
                       brier_score, log_loss, created_at, resolved_at
                FROM predictions
                WHERE {' AND '.join(clauses)}
                ORDER BY resolved_at ASC NULLS LAST, created_at ASC
                """,
                params,
            ).fetchall()

        open_excluded = sum(1 for r in rows if r[3] == "OPEN")
        voided_excluded = sum(1 for r in rows if r[3] == "VOIDED")
        resolved = [r for r in rows if r[3] == "RESOLVED" and r[6] in (0, 1)]
        n = len(resolved)

        def _mean_ci_95(vals: list[float], *, low_clamp: float | None = None, high_clamp: float | None = None) -> tuple[float | None, list[float] | None]:
            k = len(vals)
            if k == 0:
                return None, None
            mean = sum(vals) / k
            if k < 2:
                return round(mean, 6), None
            var = sum((x - mean) ** 2 for x in vals) / (k - 1)
            margin = 1.96 * math.sqrt(var / k)
            lo = mean - margin
            hi = mean + margin
            if low_clamp is not None:
                lo = max(low_clamp, lo)
            if high_clamp is not None:
                hi = min(high_clamp, hi)
            return round(mean, 6), [round(lo, 6), round(hi, 6)]

        def _wilson_ci_95_pct(successes: int, total: int) -> list[float] | None:
            if total == 0:
                return None
            z = 1.96
            phat = successes / total
            denom = 1.0 + (z * z) / total
            center = (phat + (z * z) / (2.0 * total)) / denom
            half = (z * math.sqrt((phat * (1.0 - phat) + (z * z) / (4.0 * total)) / total)) / denom
            return [round(max(0.0, (center - half) * 100.0), 2), round(min(100.0, (center + half) * 100.0), 2)]

        if n == 0:
            return {
                "provenance": "HISTORICAL",
                "resolved_count": 0,
                "open_excluded_count": open_excluded,
                "voided_excluded_count": voided_excluded,
                "min_resolved_samples_required": min_n,
                "comparative_claim_allowed": False,
                "comparative_verdict": f"INSUFFICIENT_SAMPLES (0/{min_n} resolved required)",
                "agent_metrics": {
                    "mean_brier_score": None,
                    "brier_ci_95": None,
                    "mean_log_loss": None,
                    "log_loss_ci_95": None,
                    "expected_calibration_error": None,
                    "directional_accuracy_pct": None,
                    "accuracy_wilson_ci_95_pct": None,
                },
                "baselines": {
                    "base_rate_prob": None,
                    "base_rate_brier_score": None,
                    "base_rate_log_loss": None,
                    "base_rate_accuracy_pct": None,
                    "market_implied_sample_size": 0,
                    "market_implied_brier_score": None,
                    "market_implied_log_loss": None,
                    "market_implied_accuracy_pct": None,
                    "agent_brier_on_market_subset": None,
                    "paired_brier_diff_ci_95": None,
                    "brier_skill_vs_market": None,
                },
                "by_category": [],
                "by_agent": [],
                "note": "Read-only historical backtest excluding OPEN and VOIDED forecasts.",
            }

        agent_briers: list[float] = []
        agent_lls: list[float] = []
        agent_correct = 0
        outcomes: list[int] = []
        probs: list[float] = []

        # Market-implied paired accumulators
        mkt_briers: list[float] = []
        mkt_lls: list[float] = []
        mkt_agent_briers: list[float] = []
        mkt_diff_briers: list[float] = []
        mkt_correct = 0

        by_cat_map: dict[str, dict[str, Any]] = {}
        by_agent_map: dict[str, dict[str, Any]] = {}

        for _, ag_id, cat_raw, _, p_raw, mkt_raw, y_raw, b_raw, ll_raw, _, _ in resolved:
            p = float(p_raw)
            y = int(y_raw)
            brier = float(b_raw) if b_raw is not None else compute_brier_score(p, y)
            ll = float(ll_raw) if ll_raw is not None else compute_log_loss(p, y)
            correct = 1 if ((p >= 0.5 and y == 1) or (p < 0.5 and y == 0)) else 0

            agent_briers.append(brier)
            agent_lls.append(ll)
            agent_correct += correct
            outcomes.append(y)
            probs.append(p)

            cat_acc = by_cat_map.setdefault(cat_raw, {"briers": [], "lls": [], "correct": 0, "count": 0})
            cat_acc["briers"].append(brier)
            cat_acc["lls"].append(ll)
            cat_acc["correct"] += correct
            cat_acc["count"] += 1

            ag_acc = by_agent_map.setdefault(ag_id, {"briers": [], "lls": [], "correct": 0, "count": 0})
            ag_acc["briers"].append(brier)
            ag_acc["lls"].append(ll)
            ag_acc["correct"] += correct
            ag_acc["count"] += 1

            if mkt_raw is not None:
                mp = float(mkt_raw)
                mb = compute_brier_score(mp, y)
                ml = compute_log_loss(mp, y)
                mkt_briers.append(mb)
                mkt_lls.append(ml)
                mkt_agent_briers.append(brier)
                mkt_diff_briers.append(brier - mb)
                if (mp >= 0.5 and y == 1) or (mp < 0.5 and y == 0):
                    mkt_correct += 1

        # ECE over 10 buckets
        buckets_acc: list[list[tuple[float, int]]] = [[] for _ in range(10)]
        for p, y in zip(probs, outcomes):
            idx = min(9, max(0, int(p * 10)))
            buckets_acc[idx].append((p, y))
        ece = sum(
            (len(b) / n) * abs((sum(x[0] for x in b) / len(b)) - (sum(x[1] for x in b) / len(b)))
            for b in buckets_acc
            if len(b) > 0
        )

        # Historical Base-Rate baseline
        base_rate = sum(outcomes) / n
        base_briers = [compute_brier_score(base_rate, y) for y in outcomes]
        base_lls = [compute_log_loss(base_rate, y) for y in outcomes]
        base_correct = sum(1 for y in outcomes if (base_rate >= 0.5 and y == 1) or (base_rate < 0.5 and y == 0))

        mean_brier, brier_ci = _mean_ci_95(agent_briers, low_clamp=0.0, high_clamp=1.0)
        mean_ll, ll_ci = _mean_ci_95(agent_lls, low_clamp=0.0)
        acc_pct = round((agent_correct / n) * 100.0, 2)
        acc_ci = _wilson_ci_95_pct(agent_correct, n)

        mkt_n = len(mkt_briers)
        mean_mkt_brier, _ = _mean_ci_95(mkt_briers, low_clamp=0.0, high_clamp=1.0)
        mean_mkt_ll, _ = _mean_ci_95(mkt_lls, low_clamp=0.0)
        mean_ag_on_mkt, _ = _mean_ci_95(mkt_agent_briers, low_clamp=0.0, high_clamp=1.0)
        paired_diff_mean, paired_diff_ci = _mean_ci_95(mkt_diff_briers)
        skill_vs_mkt = (
            round(float(mean_mkt_brier) - float(mean_ag_on_mkt), 6)
            if (mean_mkt_brier is not None and mean_ag_on_mkt is not None)
            else None
        )

        comparative_allowed = n >= min_n
        if not comparative_allowed:
            verdict = f"INSUFFICIENT_SAMPLES ({n}/{min_n} resolved required before comparative claims)"
        elif mkt_n >= min_n and paired_diff_ci is not None:
            if paired_diff_ci[1] < 0.0:
                verdict = "STATISTICALLY_LOWER_BRIER_THAN_MARKET_IMPLIED"
            elif paired_diff_ci[0] > 0.0:
                verdict = "MARKET_IMPLIED_OUTPERFORMS_AGENT"
            else:
                verdict = "STATISTICALLY_INDISTINGUISHABLE_FROM_MARKET_IMPLIED"
        else:
            mean_base_brier = sum(base_briers) / n
            if mean_brier is not None and mean_brier < mean_base_brier and brier_ci is not None and brier_ci[1] < mean_base_brier:
                verdict = "OUTPERFORMS_HISTORICAL_BASE_RATE (MARKET_IMPLIED_SUBSET_SMALL)"
            else:
                verdict = "INSUFFICIENT_MARKET_IMPLIED_PAIRS_OR_NEUTRAL"

        by_category = [
            {
                "market_category": cat,
                "resolved_count": st["count"],
                "mean_brier_score": round(sum(st["briers"]) / st["count"], 6),
                "mean_log_loss": round(sum(st["lls"]) / st["count"], 6),
                "accuracy_pct": round((st["correct"] / st["count"]) * 100.0, 2),
            }
            for cat, st in sorted(by_cat_map.items())
        ]
        by_agent = [
            {
                "agent_id": ag,
                "resolved_count": st["count"],
                "mean_brier_score": round(sum(st["briers"]) / st["count"], 6),
                "mean_log_loss": round(sum(st["lls"]) / st["count"], 6),
                "accuracy_pct": round((st["correct"] / st["count"]) * 100.0, 2),
                "comparative_eligible": st["count"] >= min_n,
            }
            for ag, st in sorted(by_agent_map.items())
        ]

        return {
            "provenance": "HISTORICAL",
            "resolved_count": n,
            "open_excluded_count": open_excluded,
            "voided_excluded_count": voided_excluded,
            "min_resolved_samples_required": min_n,
            "comparative_claim_allowed": comparative_allowed,
            "comparative_verdict": verdict,
            "agent_metrics": {
                "mean_brier_score": mean_brier,
                "brier_ci_95": brier_ci,
                "mean_log_loss": mean_ll,
                "log_loss_ci_95": ll_ci,
                "expected_calibration_error": round(ece, 6),
                "directional_accuracy_pct": acc_pct,
                "accuracy_wilson_ci_95_pct": acc_ci,
            },
            "baselines": {
                "base_rate_prob": round(base_rate, 6),
                "base_rate_brier_score": round(sum(base_briers) / n, 6),
                "base_rate_log_loss": round(sum(base_lls) / n, 6),
                "base_rate_accuracy_pct": round((base_correct / n) * 100.0, 2),
                "market_implied_sample_size": mkt_n,
                "market_implied_brier_score": mean_mkt_brier,
                "market_implied_log_loss": mean_mkt_ll,
                "market_implied_accuracy_pct": round((mkt_correct / mkt_n) * 100.0, 2) if mkt_n > 0 else None,
                "agent_brier_on_market_subset": mean_ag_on_mkt,
                "paired_brier_diff_mean": paired_diff_mean,
                "paired_brier_diff_ci_95": paired_diff_ci,
                "brier_skill_vs_market": skill_vs_mkt,
            },
            "by_category": by_category,
            "by_agent": by_agent,
            "note": "Read-only historical validation across RESOLVED (0/1) forecasts only. OPEN and VOIDED records are excluded.",
        }


class ConfigGovernanceManager:
    """Versioned configuration store enforcing regression self-testing and immutable Risk Gate lock."""

    def __init__(self, store: DuckDBStore, learning: LearningEngine) -> None:
        self.store = store
        self.learning = learning
        self._seed_default_configs_if_empty()

    def _row_to_config(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["config"] = json.loads(d.pop("config_json", "{}"))
        return d

    def _seed_default_configs_if_empty(self) -> None:
        with self.store._lock:
            cnt = self.store._conn.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0]
            if cnt and int(cnt) > 0:
                return
            now_iso = utc_now_iso()
            defaults = [
                (
                    "AGENT_PIPELINE",
                    {
                        "default_max_concurrency": 4,
                        "default_max_recovery_attempts": 3,
                        "deterministic_first": True,
                        "live_trading_enabled": False,
                    },
                    "Initial Stage 3C/3D baseline agent pipeline configuration",
                ),
                (
                    "MODEL_ROUTING",
                    {
                        "per_task_limit_usd": 0.01,
                        "daily_budget_usd": 0.50,
                        "deterministic_first": True,
                        "economical_model": "gemini-2.5-flash-lite",
                    },
                    "Initial cost-aware model routing limits ($0.01/task, $0.50/day)",
                ),
                (
                    "CALIBRATION_PARAMS",
                    {
                        "min_resolved_samples_for_comparison": 10,
                        "num_calibration_buckets": 10,
                        "brier_error_learning_threshold": 0.15,
                    },
                    "Initial probability calibration & agent scorecard parameters",
                ),
            ]
            for domain, cfg, reason in defaults:
                self.store._conn.execute(
                    "INSERT INTO config_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        f"CFG-{uuid.uuid4().hex[:10].upper()}",
                        domain,
                        1,
                        json.dumps(cfg),
                        "ACTIVE",
                        "system_seed",
                        "system_bootstrap",
                        None,
                        None,
                        True,
                        reason,
                        now_iso,
                        now_iso,
                    ],
                )

    def list_versions(self, *, config_domain: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if config_domain:
            clauses.append("config_domain = ?")
            params.append(config_domain.strip().upper())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM config_versions {where_sql} ORDER BY config_domain ASC, version DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_config(cols, r) for r in rows]

    def get_active_config(self, config_domain: str) -> dict[str, Any] | None:
        dom = config_domain.strip().upper()
        with self.store._lock:
            row = self.store._conn.execute(
                """
                SELECT * FROM config_versions
                WHERE config_domain = ? AND status = 'ACTIVE'
                ORDER BY version DESC LIMIT 1
                """,
                [dom],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_config(cols, row)

    def propose_config_update(
        self,
        *,
        config_domain: str,
        proposed_config: dict[str, Any],
        proposed_by: str,
        change_reason: str,
        source_cycle_id: str | None = None,
        regression_case_id: str | None = None,
    ) -> dict[str, Any]:
        dom = config_domain.strip().upper()
        if dom in FORBIDDEN_CONFIG_DOMAINS or dom not in ALLOWED_CONFIG_DOMAINS:
            raise PermissionError(
                f"Forbidden config_domain '{config_domain}': agents cannot modify Risk Gate rules, security permissions, or live trading lock"
            )
        if proposed_config.get("live_trading_enabled") is True or proposed_config.get("ai_override"):
            raise PermissionError("Safety lock: proposed configuration cannot enable live trading or AI overrides")

        # TEST-set contamination guard: block tuning prompts, models, strategy parameters, or routing from TEST split
        cfg_split = str(proposed_config.get("dataset_split") or proposed_config.get("source_split") or "").strip().upper()
        if cfg_split == "TEST":
            raise PermissionError(
                "TEST-set contamination blocked: TEST split results cannot be used to tune prompts, models, strategy parameters, or routing"
            )
        cfg_snap_id = str(proposed_config.get("dataset_snapshot_id") or "").strip()
        if cfg_snap_id:
            snap_rec = self.store.get_dataset_snapshot(cfg_snap_id)
            if snap_rec and str(snap_rec.get("split") or "").upper() == "TEST":
                raise PermissionError(
                    f"TEST-set contamination blocked: dataset_snapshot_id '{cfg_snap_id}' belongs to TEST split and cannot drive config tuning"
                )
        if source_cycle_id:
            cyc = self.learning.get_cycle(source_cycle_id)
            if cyc:
                exp = cyc.get("expected_outcome") or {}
                act = cyc.get("actual_outcome") or {}
                evs = cyc.get("evidence") or []
                cycle_splits = {
                    str(exp.get("dataset_split") or "").upper(),
                    str(act.get("dataset_split") or "").upper(),
                }
                for ev in evs:
                    if isinstance(ev, dict):
                        cycle_splits.add(str(ev.get("dataset_split") or "").upper())
                linked_pid = exp.get("linked_prediction_id") or act.get("linked_prediction_id")
                if not linked_pid and cyc.get("source_type") == "PREDICTION_ERROR":
                    linked_pid = cyc.get("source_ref_id")
                if linked_pid:
                    row_sp = self.store._conn.execute(
                        "SELECT dataset_split FROM predictions WHERE prediction_id = ?",
                        [str(linked_pid)],
                    ).fetchone()
                    if row_sp and row_sp[0]:
                        cycle_splits.add(str(row_sp[0]).upper())
                if "TEST" in cycle_splits or bool(act.get("test_split_quarantined")):
                    raise PermissionError(
                        f"TEST-set contamination blocked: learning cycle '{source_cycle_id}' references TEST split data"
                    )

        with self.store._lock:
            max_ver = self.store._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM config_versions WHERE config_domain = ?",
                [dom],
            ).fetchone()[0]
            next_ver = int(max_ver or 0) + 1
            cfg_id = f"CFG-{uuid.uuid4().hex[:10].upper()}"
            now_iso = utc_now_iso()

            reg_passed = False
            if regression_case_id:
                row = self.store._conn.execute(
                    "SELECT status FROM generated_regression_cases WHERE case_id = ?",
                    [regression_case_id],
                ).fetchone()
                reg_passed = bool(row and row[0] == "PASSING")

            self.store._conn.execute(
                "INSERT INTO config_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    cfg_id,
                    dom,
                    next_ver,
                    json.dumps(proposed_config),
                    "CANDIDATE",
                    proposed_by.strip(),
                    None,
                    source_cycle_id,
                    regression_case_id,
                    reg_passed,
                    change_reason.strip(),
                    now_iso,
                    None,
                ],
            )
            row = self.store._conn.execute(
                "SELECT * FROM config_versions WHERE config_version_id = ?",
                [cfg_id],
            ).fetchone()
            cols = [d[0] for d in self.store._conn.description]
            assert row is not None
            return self._row_to_config(cols, row)

    def activate_config_version(
        self,
        config_version_id: str,
        *,
        reviewed_by: str,
    ) -> dict[str, Any]:
        """Activate a CANDIDATE config version ONLY if independently reviewed and regression-verified."""
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM config_versions WHERE config_version_id = ?",
                [config_version_id],
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown config_version_id '{config_version_id}'")
            cols = [d[0] for d in self.store._conn.description]
            cand = self._row_to_config(cols, row)

            if cand["config_domain"] in FORBIDDEN_CONFIG_DOMAINS:
                raise PermissionError("Cannot activate forbidden Risk Gate or security domain")
            if reviewed_by.strip().lower() == cand["proposed_by"].strip().lower():
                raise PermissionError(
                    f"Self-activation denied: '{reviewed_by}' cannot approve its own config proposal"
                )

            reg_id = cand.get("regression_case_id")
            if not reg_id:
                raise ValueError("Regression test case is required before activating configuration")
            reg_row = self.store._conn.execute(
                "SELECT status FROM generated_regression_cases WHERE case_id = ?",
                [reg_id],
            ).fetchone()
            if not reg_row or reg_row[0] != "PASSING":
                raise ValueError(
                    f"Self-test gate failed closed: regression case '{reg_id}' is not PASSING"
                )

            if cand.get("source_cycle_id"):
                cyc = self.learning.get_cycle(cand["source_cycle_id"])
                if not cyc or cyc["stage"] not in ("VERIFIED_LESSON", "APPLIED"):
                    raise ValueError(
                        f"Linked learning cycle '{cand['source_cycle_id']}' is not VERIFIED_LESSON/APPLIED"
                    )

            now_iso = utc_now_iso()
            self.store._conn.execute(
                "UPDATE config_versions SET status = 'ROLLED_BACK' WHERE config_domain = ? AND status = 'ACTIVE'",
                [cand["config_domain"]],
            )
            self.store._conn.execute(
                """
                UPDATE config_versions SET
                    status = 'ACTIVE',
                    reviewed_by = ?,
                    regression_passed = TRUE,
                    activated_at = ?
                WHERE config_version_id = ?
                """,
                [reviewed_by.strip(), now_iso, config_version_id],
            )
            updated = self.store._conn.execute(
                "SELECT * FROM config_versions WHERE config_version_id = ?",
                [config_version_id],
            ).fetchone()
            assert updated is not None
            return self._row_to_config(cols, updated)

    def rollback_config(
        self,
        *,
        config_domain: str,
        target_version: int,
        actor: str = "operator",
        reason: str = "Operator configuration rollback",
    ) -> dict[str, Any]:
        dom = config_domain.strip().upper()
        if dom in FORBIDDEN_CONFIG_DOMAINS or dom not in ALLOWED_CONFIG_DOMAINS:
            raise PermissionError(f"Unsupported or forbidden config_domain '{config_domain}'")

        with self.store._lock:
            snap_row = self.store._conn.execute(
                "SELECT * FROM config_versions WHERE config_domain = ? AND version = ?",
                [dom, int(target_version)],
            ).fetchone()
            if not snap_row:
                raise ValueError(f"Version {target_version} not found for domain '{dom}'")
            cols = [d[0] for d in self.store._conn.description]
            snap = self._row_to_config(cols, snap_row)
            if snap.get("config", {}).get("live_trading_enabled") is True or snap.get("config", {}).get("ai_override"):
                raise PermissionError("Safety lock: cannot rollback to a configuration enabling live trading or AI override")
            if not bool(snap.get("regression_passed", False)):
                raise ValueError(
                    f"Self-test gate failed closed: version {target_version} for '{dom}' has not passed regression verification"
                )

            max_ver = self.store._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM config_versions WHERE config_domain = ?",
                [dom],
            ).fetchone()[0]
            new_ver = int(max_ver or 0) + 1
            new_id = f"CFG-{uuid.uuid4().hex[:10].upper()}"
            now_iso = utc_now_iso()

            self.store._conn.execute(
                "UPDATE config_versions SET status = 'ROLLED_BACK' WHERE config_domain = ? AND status = 'ACTIVE'",
                [dom],
            )
            self.store._conn.execute(
                "INSERT INTO config_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    new_id,
                    dom,
                    new_ver,
                    json.dumps(snap["config"]),
                    "ACTIVE",
                    actor.strip(),
                    actor.strip(),
                    snap.get("source_cycle_id"),
                    snap.get("regression_case_id"),
                    bool(snap.get("regression_passed", True)),
                    f"Rollback to v{target_version}: {reason.strip()}",
                    now_iso,
                    now_iso,
                ],
            )
            restored = self.store._conn.execute(
                "SELECT * FROM config_versions WHERE config_version_id = ?",
                [new_id],
            ).fetchone()
            assert restored is not None
            return self._row_to_config(cols, restored)
