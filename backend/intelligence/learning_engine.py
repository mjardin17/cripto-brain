"""Closed-loop Learning Lifecycle Engine & Regression Case Generator (Stage 3B).

Enforces the 7-stage deterministic learning pipeline:
  OBSERVATION -> EVALUATION -> DECISION -> OUTCOME -> REVIEW -> VERIFIED_LESSON -> APPLIED
  (with fail-closed branches to REJECTED or CONTRADICTED)

Invariants:
- Every learning cycle links back to its original source_type and source_ref_id.
- Every promoted lesson requires verifiable supporting evidence and independent review
  (an agent cannot promote its own unverified conclusions).
- Unverified or untested lessons can never mutate production agent behavior.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from backend.intelligence.memory_manager import (
    MemoryTier,
    StructuredMemoryManager,
    has_verifiable_evidence,
    normalize_evidence_list,
)
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

LearningStage = Literal[
    "OBSERVATION",
    "EVALUATION",
    "DECISION",
    "OUTCOME",
    "REVIEW",
    "VERIFIED_LESSON",
    "APPLIED",
    "REJECTED",
    "CONTRADICTED",
    "ROLLED_BACK",
]


class LearningEngine:
    """Manages end-to-end learning cycles, mistake tracking, and regression test generation."""

    def __init__(self, store: DuckDBStore, memory_manager: StructuredMemoryManager) -> None:
        self.store = store
        self.memory = memory_manager

    def _row_to_cycle(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["expected_outcome"] = json.loads(d.pop("expected_outcome_json", "{}"))
        d["actual_outcome"] = json.loads(d.pop("actual_outcome_json", "{}"))
        d["evidence"] = json.loads(d.pop("evidence_json", "[]"))
        return d

    def _row_to_regression(self, cols: list[str], row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(cols, row))
        d["input_fixture"] = json.loads(d.pop("input_fixture_json", "{}"))
        d["expected_invariant"] = json.loads(d.pop("expected_invariant_json", "{}"))
        return d

    def get_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM learning_cycles WHERE cycle_id = ?",
                [cycle_id],
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self.store._conn.description]
            return self._row_to_cycle(cols, row)

    def list_cycles(
        self,
        *,
        stage: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if stage:
            clauses.append("stage = ?")
            params.append(stage.upper())
        if source_type:
            clauses.append("source_type = ?")
            params.append(source_type.upper())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM learning_cycles {where_sql} ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.store._lock:
            rows = self.store._conn.execute(sql, params).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_cycle(cols, r) for r in rows]

    # ------------------------------------------------------------------
    # Stage 1: OBSERVATION
    # ------------------------------------------------------------------
    def record_observation(
        self,
        *,
        source_type: str,
        source_ref_id: str,
        agent_id: str,
        subject_key: str,
        hypothesis: str,
        expected_outcome: dict[str, Any],
        evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not source_ref_id.strip():
            raise ValueError("source_ref_id is required to link learning cycle to original event")
        cycle_id = f"LCYC-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()
        ev_list = normalize_evidence_list(evidence)
        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO learning_cycles VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    cycle_id,
                    "OBSERVATION",
                    source_type.upper(),
                    source_ref_id.strip(),
                    agent_id.strip(),
                    subject_key.strip().lower(),
                    hypothesis.strip(),
                    json.dumps(expected_outcome or {}),
                    "",
                    json.dumps({}),
                    None,
                    "",
                    json.dumps(ev_list),
                    None,
                    None,
                    None,
                    False,
                    now_iso,
                    now_iso,
                ],
            )
        cycle = self.get_cycle(cycle_id)
        assert cycle is not None
        return cycle

    # ------------------------------------------------------------------
    # Stage 2: EVALUATION
    # ------------------------------------------------------------------
    def record_evaluation(
        self,
        cycle_id: str,
        *,
        evaluation_notes: str,
        evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.store._lock:
            cycle = self.get_cycle(cycle_id)
            if not cycle:
                raise ValueError(f"Unknown cycle_id '{cycle_id}'")
            if cycle["stage"] != "OBSERVATION":
                raise ValueError(f"Expected stage OBSERVATION, got '{cycle['stage']}'")

            merged_ev = list(cycle["evidence"]) + normalize_evidence_list(evidence)
            now_iso = utc_now_iso()
            self.store._conn.execute(
                """
                UPDATE learning_cycles SET
                    stage = 'EVALUATION',
                    review_notes = ?,
                    evidence_json = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                [evaluation_notes.strip(), json.dumps(merged_ev), now_iso, cycle_id],
            )
        updated = self.get_cycle(cycle_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------
    # Stage 3: DECISION
    # ------------------------------------------------------------------
    def record_decision(self, cycle_id: str, *, decision_summary: str) -> dict[str, Any]:
        with self.store._lock:
            cycle = self.get_cycle(cycle_id)
            if not cycle:
                raise ValueError(f"Unknown cycle_id '{cycle_id}'")
            if cycle["stage"] not in ("OBSERVATION", "EVALUATION"):
                raise ValueError(f"Cannot record decision from stage '{cycle['stage']}'")
            now_iso = utc_now_iso()
            self.store._conn.execute(
                """
                UPDATE learning_cycles SET
                    stage = 'DECISION',
                    decision_summary = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                [decision_summary.strip(), now_iso, cycle_id],
            )
        updated = self.get_cycle(cycle_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------
    # Stage 4: OUTCOME (Expected vs Actual comparison)
    # ------------------------------------------------------------------
    def record_outcome(
        self,
        cycle_id: str,
        *,
        actual_outcome: dict[str, Any],
        outcome_matched: bool | None,
        evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.store._lock:
            cycle = self.get_cycle(cycle_id)
            if not cycle:
                raise ValueError(f"Unknown cycle_id '{cycle_id}'")
            if cycle["stage"] != "DECISION":
                raise ValueError(f"Expected stage DECISION before OUTCOME, got '{cycle['stage']}'")

            merged_ev = list(cycle["evidence"]) + normalize_evidence_list(evidence)
            now_iso = utc_now_iso()
            stored_match: bool | None = None if outcome_matched is None else bool(outcome_matched)
            self.store._conn.execute(
                """
                UPDATE learning_cycles SET
                    stage = 'OUTCOME',
                    actual_outcome_json = ?,
                    outcome_matched = ?,
                    evidence_json = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                [
                    json.dumps(actual_outcome or {}),
                    stored_match,
                    json.dumps(merged_ev),
                    now_iso,
                    cycle_id,
                ],
            )

            # Log recurring mistakes into AGENT_PERF memory ONLY when expected != actual (explicit False).
            # Inconclusive / VOID outcomes (outcome_matched is None) must never train mistake memory.
            if stored_match is False:
                self.memory.record_candidate(
                    memory_tier="AGENT_PERF",
                    category="RECURRING_MISTAKE",
                    title=f"Outcome mismatch on {cycle['subject_key']}",
                    summary=(
                        f"Expected {cycle['expected_outcome']} differed from actual {actual_outcome} "
                        f"(source {cycle['source_type']}:{cycle['source_ref_id']})."
                    ),
                    source_agent=cycle["agent_id"],
                    explicit_key=f"agent_perf:mistake:{cycle['subject_key']}",
                    content={
                        "subject_key": cycle["subject_key"],
                        "last_cycle_id": cycle_id,
                        "expected": cycle["expected_outcome"],
                        "actual": actual_outcome,
                    },
                    evidence=merged_ev
                    or [
                        {
                            "source_type": cycle["source_type"],
                            "source_ref_id": cycle["source_ref_id"],
                            "detail": "Expected vs actual mismatch recorded at OUTCOME stage",
                        }
                    ],
                    tags=["mistake", cycle["source_type"].lower(), cycle["subject_key"]],
                    confidence=0.72,
                )

        updated = self.get_cycle(cycle_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------
    # Stages 5 & 6: REVIEW -> VERIFIED_LESSON / REJECTED / CONTRADICTED
    # ------------------------------------------------------------------
    def review_and_extract_lesson(
        self,
        cycle_id: str,
        *,
        reviewer_id: str,
        review_notes: str,
        approve_lesson: bool,
        lesson_tier: MemoryTier = "PROCEDURAL",
        lesson_category: str = "VERIFIED_LESSON",
        lesson_title: str = "",
        lesson_summary: str = "",
        claim_key: str | None = None,
        claim_value: str | None = None,
        stance: str | None = None,
        confidence: float = 0.80,
        additional_evidence: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Independent review stage: rejects unverified/self-promoted conclusions or promotes verified lessons."""
        with self.store._lock:
            cycle = self.get_cycle(cycle_id)
            if not cycle:
                raise ValueError(f"Unknown cycle_id '{cycle_id}'")
            if cycle["stage"] != "OUTCOME":
                raise ValueError(f"Expected stage OUTCOME before REVIEW, got '{cycle['stage']}'")

            if reviewer_id.strip().lower() == cycle["agent_id"].strip().lower():
                raise PermissionError(
                    f"Self-promotion denied: agent '{reviewer_id}' cannot review/promote its own learning cycle '{cycle_id}'"
                )

            merged_ev = list(cycle["evidence"]) + normalize_evidence_list(additional_evidence)
            now_iso = utc_now_iso()

            # Stage 5: mark REVIEW snapshot first
            self.store._conn.execute(
                """
                UPDATE learning_cycles SET
                    stage = 'REVIEW',
                    review_notes = ?,
                    evidence_json = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                [review_notes.strip(), json.dumps(merged_ev), now_iso, cycle_id],
            )

            title = lesson_title.strip() or f"Lesson for {cycle['subject_key']}"
            summary = lesson_summary.strip() or cycle["hypothesis"]
            cand_content: dict[str, Any] = {
                "subject_key": cycle["subject_key"],
                "source_cycle_id": cycle_id,
                "source_type": cycle["source_type"],
                "source_ref_id": cycle["source_ref_id"],
            }
            if claim_key:
                cand_content["claim_key"] = claim_key
                cand_content["claim_value"] = claim_value
            if stance:
                cand_content["stance"] = stance

            cand_res = self.memory.record_candidate(
                memory_tier=lesson_tier,
                category=lesson_category,
                title=title,
                summary=summary,
                source_agent=cycle["agent_id"],
                explicit_key=f"{lesson_tier.lower()}:{lesson_category.lower()}:{cycle['subject_key']}",
                content=cand_content,
                evidence=merged_ev,
                tags=[cycle["source_type"].lower(), cycle["subject_key"], "learning_cycle"],
                confidence=confidence,
            )
            cand_entry = cand_res["entry"]
            cand_id = cand_entry["memory_id"]

            if cand_res.get("contradiction_detected"):
                self.store._conn.execute(
                    """
                    UPDATE learning_cycles SET
                        stage = 'CONTRADICTED',
                        candidate_memory_id = ?,
                        review_notes = ?,
                        updated_at = ?
                    WHERE cycle_id = ?
                    """,
                    [
                        cand_id,
                        f"{review_notes} | Contradiction: {cand_res.get('contradiction_reason')}",
                        utc_now_iso(),
                        cycle_id,
                    ],
                )
                return self.get_cycle(cycle_id)  # type: ignore[return-value]

            if not approve_lesson or not has_verifiable_evidence(cand_entry["evidence"]):
                self.memory.update_entry(
                    cand_id,
                    changed_by=reviewer_id,
                    change_reason=f"Rejected during review: {'missing verifiable evidence' if not has_verifiable_evidence(cand_entry['evidence']) else review_notes}",
                    status="REJECTED",
                )
                self.store._conn.execute(
                    """
                    UPDATE learning_cycles SET
                        stage = 'REJECTED',
                        candidate_memory_id = ?,
                        review_notes = ?,
                        updated_at = ?
                    WHERE cycle_id = ?
                    """,
                    [
                        cand_id,
                        review_notes
                        if approve_lesson is False
                        else f"Rejected: supporting evidence required before VERIFIED_LESSON ({review_notes})",
                        utc_now_iso(),
                        cycle_id,
                    ],
                )
                return self.get_cycle(cycle_id)  # type: ignore[return-value]

            # Promote candidate -> VERIFIED_LESSON via independent reviewer
            promoted = self.memory.verify_and_promote(
                cand_id,
                reviewer_id=reviewer_id,
                review_reason=review_notes,
                additional_evidence=merged_ev,
            )
            self.store._conn.execute(
                """
                UPDATE learning_cycles SET
                    stage = 'VERIFIED_LESSON',
                    candidate_memory_id = ?,
                    promoted_memory_id = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                [cand_id, promoted["memory_id"], utc_now_iso(), cycle_id],
            )
            return self.get_cycle(cycle_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Stage 7: FUTURE APPLICATION (Requires Verified + Tested Guard)
    # ------------------------------------------------------------------
    def apply_verified_lesson(
        self,
        cycle_id: str,
        *,
        target_workflow: str,
        regression_verified: bool = False,
    ) -> dict[str, Any]:
        """Mark a verified & tested lesson applied to future workflows (fail-closed if untested)."""
        with self.store._lock:
            cycle = self.get_cycle(cycle_id)
            if not cycle:
                raise ValueError(f"Unknown cycle_id '{cycle_id}'")
            if cycle["stage"] != "VERIFIED_LESSON" or not cycle.get("promoted_memory_id"):
                raise ValueError(
                    f"Cannot apply cycle '{cycle_id}': stage must be VERIFIED_LESSON (got '{cycle['stage']}')"
                )
            mem = self.memory.get_entry(cycle["promoted_memory_id"])
            if not mem or mem["status"] != "VERIFIED_LESSON":
                raise ValueError(
                    f"Cannot apply cycle '{cycle_id}': memory '{cycle.get('promoted_memory_id')}' is not VERIFIED_LESSON"
                )

            has_passing_reg = False
            if cycle.get("regression_case_id"):
                row = self.store._conn.execute(
                    "SELECT status FROM generated_regression_cases WHERE case_id = ?",
                    [cycle["regression_case_id"]],
                ).fetchone()
                has_passing_reg = bool(row and row[0] == "PASSING")

            if not (regression_verified or has_passing_reg):
                raise ValueError(
                    "Fail-closed: cannot apply lesson to future production behavior without passing regression/verification test"
                )

            now_iso = utc_now_iso()
            self.store._conn.execute(
                "UPDATE memory_entries SET last_applied_at = ?, updated_at = ? WHERE memory_id = ?",
                [now_iso, now_iso, mem["memory_id"]],
            )
            self.store._conn.execute(
                """
                UPDATE learning_cycles SET
                    stage = 'APPLIED',
                    applied_flag = TRUE,
                    decision_summary = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                [
                    f"{cycle['decision_summary']} | Applied to '{target_workflow}'",
                    now_iso,
                    cycle_id,
                ],
            )
            return self.get_cycle(cycle_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Stages 9 & 10: CONTINUOUS REGRESSION MONITORING & ROLLBACK
    # ------------------------------------------------------------------
    def rollback_lesson(
        self,
        cycle_id: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        """Roll back an APPLIED or VERIFIED_LESSON cycle after regression detection or operator audit."""
        if not reason or len(reason.strip()) < 3:
            raise ValueError("Rollback reason is required")
        with self.store._lock:
            cycle = self.get_cycle(cycle_id)
            if not cycle:
                raise ValueError(f"Unknown cycle_id '{cycle_id}'")
            if cycle["stage"] not in ("APPLIED", "VERIFIED_LESSON"):
                raise ValueError(
                    f"Cannot roll back cycle '{cycle_id}' from stage '{cycle['stage']}'"
                )

            now_iso = utc_now_iso()
            promoted_id = cycle.get("promoted_memory_id")
            if promoted_id:
                mem = self.memory.get_entry(promoted_id)
                if mem:
                    self.memory.update_entry(
                        promoted_id,
                        changed_by=actor,
                        change_reason=f"Rolled back learning cycle {cycle_id}: {reason.strip()}",
                        status="ARCHIVED",
                    )

            self.store._conn.execute(
                """
                UPDATE learning_cycles SET
                    stage = 'ROLLED_BACK',
                    applied_flag = FALSE,
                    review_notes = ?,
                    updated_at = ?
                WHERE cycle_id = ?
                """,
                [
                    f"{cycle['review_notes']} | ROLLED_BACK by {actor}: {reason.strip()}".strip(" |"),
                    now_iso,
                    cycle_id,
                ],
            )
            self.store.record_system_event(
                severity="WARNING",
                component="learning_engine",
                event_type="LESSON_ROLLED_BACK",
                message=f"Rolled back cycle {cycle_id} (actor={actor}): {reason.strip()}",
                detail={"cycle_id": cycle_id, "promoted_memory_id": promoted_id},
            )
            return self.get_cycle(cycle_id)  # type: ignore[return-value]

    def monitor_regressions(self, *, actor: str = "learning_monitor") -> dict[str, Any]:
        """Monitor APPLIED/VERIFIED cycles whose linked regression case is FAILING and roll them back safely."""
        rolled_back_ids: list[str] = []
        failing_cases: list[str] = []
        with self.store._lock:
            rows = self.store._conn.execute(
                """
                SELECT lc.cycle_id, rc.case_id, rc.last_run_detail
                FROM learning_cycles lc
                JOIN generated_regression_cases rc ON lc.regression_case_id = rc.case_id
                WHERE lc.stage IN ('APPLIED', 'VERIFIED_LESSON')
                  AND rc.status = 'FAILING'
                """
            ).fetchall()
            for cycle_id, case_id, detail in rows:
                failing_cases.append(str(case_id))
                self.rollback_lesson(
                    str(cycle_id),
                    actor=actor,
                    reason=f"Regression case {case_id} transitioned to FAILING ({detail or 'invariant mismatch'})",
                )
                rolled_back_ids.append(str(cycle_id))
        return {
            "checked_at": utc_now_iso(),
            "failing_cases_count": len(failing_cases),
            "failing_case_ids": failing_cases,
            "rolled_back_cycle_ids": rolled_back_ids,
        }

    # ------------------------------------------------------------------
    # Regression Case Generator & Mistake/Recovery Helpers (Priority 4)
    # ------------------------------------------------------------------
    def generate_regression_case(
        self,
        *,
        source_ref_id: str,
        category: str,
        title: str,
        description: str,
        input_fixture: dict[str, Any],
        expected_invariant: dict[str, Any],
        source_memory_id: str | None = None,
        source_cycle_id: str | None = None,
    ) -> dict[str, Any]:
        case_id = f"REGC-{uuid.uuid4().hex[:10].upper()}"
        now_iso = utc_now_iso()
        with self.store._lock:
            self.store._conn.execute(
                """
                INSERT INTO generated_regression_cases VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                [
                    case_id,
                    source_memory_id,
                    source_cycle_id,
                    source_ref_id,
                    category.upper(),
                    title.strip(),
                    description.strip(),
                    json.dumps(input_fixture or {}),
                    json.dumps(expected_invariant or {}),
                    "GENERATED",
                    None,
                    None,
                    None,
                    now_iso,
                ],
            )
            if source_cycle_id:
                self.store._conn.execute(
                    "UPDATE learning_cycles SET regression_case_id = ?, updated_at = ? WHERE cycle_id = ?",
                    [case_id, now_iso, source_cycle_id],
                )
            row = self.store._conn.execute(
                "SELECT * FROM generated_regression_cases WHERE case_id = ?",
                [case_id],
            ).fetchone()
            cols = [d[0] for d in self.store._conn.description]
            assert row is not None
            return self._row_to_regression(cols, row)

    def run_regression_case(
        self,
        case_id: str,
        *,
        observed_output: dict[str, Any],
    ) -> dict[str, Any]:
        """Deterministically evaluate observed_output against expected_invariant key/value constraints."""
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM generated_regression_cases WHERE case_id = ?",
                [case_id],
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown regression case_id '{case_id}'")
            cols = [d[0] for d in self.store._conn.description]
            case = self._row_to_regression(cols, row)

            expected = case["expected_invariant"]
            mismatches: list[str] = []
            for key, exp_val in expected.items():
                if key not in observed_output:
                    mismatches.append(f"Missing key '{key}'")
                elif observed_output[key] != exp_val:
                    mismatches.append(f"'{key}': expected {exp_val!r}, got {observed_output[key]!r}")

            passed = len(mismatches) == 0
            status = "PASSING" if passed else "FAILING"
            detail = "All expected invariants matched" if passed else "; ".join(mismatches)
            now_iso = utc_now_iso()
            self.store._conn.execute(
                """
                UPDATE generated_regression_cases SET
                    status = ?,
                    last_run_status = ?,
                    last_run_detail = ?,
                    last_run_at = ?
                WHERE case_id = ?
                """,
                [status, status, detail, now_iso, case_id],
            )
            updated_row = self.store._conn.execute(
                "SELECT * FROM generated_regression_cases WHERE case_id = ?",
                [case_id],
            ).fetchone()
            assert updated_row is not None
            return self._row_to_regression(cols, updated_row)

    def list_regression_cases(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM generated_regression_cases ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self._row_to_regression(cols, r) for r in rows]

    def get_recurring_mistakes(self, min_occurrences: int = 2) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._conn.execute(
                """
                SELECT * FROM memory_entries
                WHERE category = 'RECURRING_MISTAKE'
                  AND occurrence_count >= ?
                  AND status NOT IN ('ARCHIVED', 'SUPERSEDED')
                ORDER BY occurrence_count DESC, updated_at DESC
                """,
                [int(min_occurrences)],
            ).fetchall()
            cols = [d[0] for d in self.store._conn.description]
            return [self.memory._row_to_entry(cols, r) for r in rows]

    def record_prediction_error_learning(
        self,
        *,
        prediction_id: str,
        agent_id: str,
        subject_key: str,
        predicted_prob: float,
        actual_outcome: int,
        reviewer_id: str = "learning_reviewer",
    ) -> dict[str, Any]:
        """Convenience pipeline capturing wrong predictions into learning_cycles + AGENT_PERF memory."""
        brier = round((float(predicted_prob) - int(actual_outcome)) ** 2, 6)
        cycle = self.record_observation(
            source_type="PREDICTION",
            source_ref_id=prediction_id,
            agent_id=agent_id,
            subject_key=subject_key,
            hypothesis=f"Predicted p={predicted_prob:.3f} for {subject_key}",
            expected_outcome={"predicted_prob": round(float(predicted_prob), 4), "brier_threshold": 0.15},
            evidence=[
                {
                    "source_type": "PROBABILITY_PREDICTION",
                    "source_ref_id": prediction_id,
                    "detail": f"predicted={predicted_prob:.4f}, resolved={actual_outcome}, brier={brier:.4f}",
                }
            ],
        )
        self.record_evaluation(cycle["cycle_id"], evaluation_notes=f"Resolved outcome={actual_outcome}, Brier={brier:.4f}")
        self.record_decision(cycle["cycle_id"], decision_summary="Logged advisory forecast and evaluated at resolution")
        matched = brier <= 0.15
        self.record_outcome(
            cycle["cycle_id"],
            actual_outcome={"resolved_outcome": int(actual_outcome), "brier_score": brier},
            outcome_matched=matched,
        )
        return self.review_and_extract_lesson(
            cycle["cycle_id"],
            reviewer_id=reviewer_id,
            review_notes=f"Calibration post-mortem (Brier={brier:.4f})",
            approve_lesson=True,
            lesson_tier="AGENT_PERF",
            lesson_category="CALIBRATION_NOTE",
            lesson_title=f"Calibration review for {subject_key}",
            lesson_summary=f"Forecast p={predicted_prob:.3f} vs outcome={actual_outcome} yielded Brier={brier:.4f}.",
            confidence=0.82,
        )

    def record_recovery_learning(
        self,
        *,
        blocker_id: str,
        agent_id: str,
        subject_key: str,
        strategy_name: str,
        succeeded: bool,
        input_fixture: dict[str, Any],
        expected_invariant: dict[str, Any],
        reviewer_id: str = "learning_reviewer",
    ) -> dict[str, Any]:
        """Record a blocker recovery outcome and generate a regression test if verified."""
        cycle = self.record_observation(
            source_type="RECOVERY",
            source_ref_id=blocker_id,
            agent_id=agent_id,
            subject_key=subject_key,
            hypothesis=f"Recovery strategy '{strategy_name}' resolves blocker '{subject_key}'",
            expected_outcome={"recovered": True, "strategy": strategy_name},
            evidence=[
                {
                    "source_type": "BLOCKER_RECOVERY",
                    "source_ref_id": blocker_id,
                    "detail": f"Strategy={strategy_name}, succeeded={succeeded}",
                }
            ],
        )
        self.record_evaluation(cycle["cycle_id"], evaluation_notes="Evaluated safety & deterministic preconditions")
        self.record_decision(cycle["cycle_id"], decision_summary=f"Executed recovery strategy '{strategy_name}'")
        self.record_outcome(
            cycle["cycle_id"],
            actual_outcome={"recovered": bool(succeeded), "strategy": strategy_name},
            outcome_matched=bool(succeeded),
        )
        reviewed = self.review_and_extract_lesson(
            cycle["cycle_id"],
            reviewer_id=reviewer_id,
            review_notes=f"Recovery verification outcome={'PASS' if succeeded else 'FAIL'}",
            approve_lesson=bool(succeeded),
            lesson_tier="PROCEDURAL",
            lesson_category="RECOVERY_STRATEGY",
            lesson_title=f"Verified recovery for {subject_key}: {strategy_name}",
            lesson_summary=f"Strategy '{strategy_name}' resolved incident {blocker_id} safely.",
            confidence=0.88 if succeeded else 0.45,
        )
        if reviewed["stage"] == "VERIFIED_LESSON":
            self.generate_regression_case(
                source_memory_id=reviewed["promoted_memory_id"],
                source_cycle_id=reviewed["cycle_id"],
                source_ref_id=blocker_id,
                category="BLOCKER_RECOVERY",
                title=f"Regression guard: {subject_key}",
                description=f"Verify recovery strategy '{strategy_name}' preserves safety invariants.",
                input_fixture=input_fixture,
                expected_invariant=expected_invariant,
            )
            return self.get_cycle(reviewed["cycle_id"])  # type: ignore[return-value]
        return reviewed
