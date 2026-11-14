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
import uuid
from typing import Any

from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.memory_manager import normalize_evidence_list
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

FORBIDDEN_CONFIG_DOMAINS = {"RISK_GATE", "RISK_POLICY", "SECURITY_PERMISSIONS", "LIVE_TRADING"}
ALLOWED_CONFIG_DOMAINS = {"AGENT_PIPELINE", "MODEL_ROUTING", "CALIBRATION_PARAMS"}


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
    ) -> dict[str, Any]:
        """Resolve an OPEN prediction cleanly; voided predictions never count as confirmed wins/losses."""
        with self.store._lock:
            pred = self.get_prediction(prediction_id)
            if not pred:
                raise ValueError(f"Unknown prediction_id '{prediction_id}'")
            if pred["status"] != "OPEN":
                raise ValueError(f"Prediction '{prediction_id}' already finalized with status '{pred['status']}'")

            now_iso = utc_now_iso()
            if isinstance(outcome, str) and outcome.strip().upper() == "VOID":
                self.store._conn.execute(
                    """
                    UPDATE predictions SET
                        status = 'VOIDED',
                        resolved_at = ?
                    WHERE prediction_id = ?
                    """,
                    [now_iso, prediction_id],
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
            direction_wrong = (pred["predicted_prob"] >= 0.5 and y == 0) or (
                pred["predicted_prob"] < 0.5 and y == 1
            )
            cycle_id: str | None = None
            if brier > 0.15 or direction_wrong:
                eff_reviewer = (
                    reviewer_id
                    if reviewer_id.strip().lower() != pred["agent_id"].strip().lower()
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
                    "source_ref_id": resolution_source,
                    "actual_outcome": y,
                    "brier_score": brier,
                    "log_loss": ll,
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
