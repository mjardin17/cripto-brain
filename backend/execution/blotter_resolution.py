"""Atomic Paper Blotter Outcome Resolution Service.

Enforces strict correctness invariants when confirming YES / NO / VOID market
outcomes on simulated paper blotter ledger entries:

1. YES/NO outcomes are evaluated strictly relative to the linked forecast's
   binary YES proposition (never inferred from paper trade buy/sell legs).
2. VOID outcomes never receive binary Brier/log-loss scores and never train
   Learning Core mistake memory (`outcome_matched = None`).
3. Linked forecasts are validated fail-closed against:
   - existence & OPEN status (cannot resolve twice or link an already-linked forecast)
   - market identifier match against the blotter entry's pair / order / position refs
   - non-inverted cross-venue polarity
   - timeframe / lookahead chronology
   - authoritative Arena model attribution (derived solely from the forecast record;
     manual input can never override or fabricate Arena model attribution)
4. Every multi-step write across DuckDB, ProbabilityIntelligenceEngine, and
   LearningEngine runs inside a single atomic `store.transaction()` so a failure
   at any stage rolls back completely with zero partial updates or duplicate cycles.
5. Resolved entries and forecasts retain structured settlement evidence, source,
   provenance, timestamps, and confirming operator identity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.intelligence.learning_engine import LearningEngine
from backend.intelligence.probability import ProbabilityIntelligenceEngine, parse_iso_utc
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso


class BlotterResolutionError(Exception):
    """Typed HTTP-mapped validation/conflict error for blotter resolution."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _try_parse_iso(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    try:
        return parse_iso_utc(raw)
    except ValueError:
        return None


def _resolve_authoritative_model_attribution(
    prediction: dict[str, Any],
    manual_arena_model_id: str | None,
) -> tuple[str, str]:
    """Derive canonical (agent_id, arena_model_id) strictly from the linked forecast record.

    If caller also supplied manual_arena_model_id, reject any mismatch so model
    attribution can never be falsely reassigned.
    """
    canonical_agent = str(prediction["agent_id"]).strip()
    forecast_model = str(prediction.get("model_id") or "").strip()
    forecast_provider = str(prediction.get("provider") or "").strip().lower()

    allowed_aliases = {canonical_agent.lower()}
    if forecast_model:
        allowed_aliases.add(forecast_model.lower())
        if forecast_provider:
            allowed_aliases.add(f"{forecast_provider}/{forecast_model}".lower())
            allowed_aliases.add(f"arena:{forecast_provider}/{forecast_model}".lower())
        allowed_aliases.add(f"arena:{forecast_model}".lower())

    if manual_arena_model_id:
        if manual_arena_model_id.strip().lower() not in allowed_aliases:
            raise BlotterResolutionError(
                409,
                (
                    f"Arena model attribution mismatch: linked forecast '{prediction['prediction_id']}' "
                    f"belongs to '{canonical_agent}', cannot attribute to '{manual_arena_model_id.strip()}'."
                ),
            )

    authoritative_arena_id = (
        canonical_agent
        if prediction.get("source_system") == "AI_ORCHESTRA_ARENA"
        or canonical_agent.lower().startswith("arena:")
        or bool(forecast_model)
        else canonical_agent
    )
    return canonical_agent, authoritative_arena_id


class PaperBlotterResolutionService:
    """Coordinates fail-closed validation and single-transaction resolution across engines."""

    def __init__(
        self,
        *,
        store: DuckDBStore,
        learning_engine: LearningEngine,
        prob_engine: ProbabilityIntelligenceEngine,
    ) -> None:
        self.store = store
        self.learning_engine = learning_engine
        self.prob_engine = prob_engine

    def resolve_entry(
        self,
        *,
        ledger_id: str,
        resolution_result: str,
        operator_username: str,
        notes: str = "",
        resolution_source: str = "",
        linked_prediction_id: str | None = None,
        arena_model_id: str | None = None,
        expected_timeframe: str | None = None,
    ) -> dict[str, Any]:
        clean_ledger_id = (ledger_id or "").strip()
        if not clean_ledger_id or len(clean_ledger_id) > 80:
            raise BlotterResolutionError(400, "Invalid paper blotter ledger_id.")

        norm_result = (resolution_result or "").strip().upper()
        if norm_result not in ("YES", "NO", "VOID"):
            raise BlotterResolutionError(422, "resolution_result must be one of YES, NO, or VOID.")

        linked_pred_id = (linked_prediction_id or "").strip() or None
        manual_arena_model = (arena_model_id or "").strip() or None
        notes_clean = (notes or "").strip()[:600]
        source_clean = (
            (resolution_source or "").strip()[:160]
            or f"operator_manual_confirmation:{clean_ledger_id}"
        )
        expected_tf_clean = (expected_timeframe or "").strip() or None
        resolved_at = utc_now_iso()
        resolved_dt = parse_iso_utc(resolved_at, field_name="resolved_at")

        # Manual Arena model attribution without a linked forecast is strictly forbidden.
        if manual_arena_model and not linked_pred_id:
            raise BlotterResolutionError(
                400,
                (
                    "Arena model attribution requires a linked forecast record; "
                    "cannot manually assign an Arena model without linking an actual forecast."
                ),
            )

        with self.store.transaction():
            entry = self.store.get_paper_pnl_entry(clean_ledger_id)
            if not entry:
                raise BlotterResolutionError(
                    404,
                    f"Unknown paper blotter ledger_id '{clean_ledger_id}'.",
                )
            if entry["resolution_result"] != "PENDING":
                raise BlotterResolutionError(
                    409,
                    (
                        f"Paper blotter entry '{clean_ledger_id}' is already confirmed as "
                        f"'{entry['resolution_result']}'."
                    ),
                )
            if self.store.has_learning_cycle_for_source("PAPER_BLOTTER_RESOLUTION", clean_ledger_id):
                raise BlotterResolutionError(
                    409,
                    f"A learning cycle already exists for paper blotter entry '{clean_ledger_id}'.",
                )

            existing_pred: dict[str, Any] | None = None
            authoritative_agent_id = "paper_execution_desk"
            authoritative_arena_model: str | None = None

            if linked_pred_id:
                existing_pred = self.prob_engine.get_prediction(linked_pred_id)
                if not existing_pred:
                    raise BlotterResolutionError(
                        404,
                        f"Unknown linked_prediction_id '{linked_pred_id}'.",
                    )
                if existing_pred["status"] != "OPEN":
                    raise BlotterResolutionError(
                        409,
                        (
                            f"Linked prediction '{linked_pred_id}' is already finalized "
                            f"with status '{existing_pred['status']}'."
                        ),
                    )
                prior_link = self.store.get_paper_pnl_by_linked_prediction(linked_pred_id)
                if prior_link is not None:
                    raise BlotterResolutionError(
                        409,
                        (
                            f"Linked prediction '{linked_pred_id}' is already associated with "
                            f"paper blotter entry '{prior_link['ledger_id']}'."
                        ),
                    )

                # Market match validation against blotter entry's pair & order/position market_refs
                valid_market_refs = self.store.list_paper_market_refs_for_execution(
                    entry["execution_id"], entry["pair_id"]
                )
                valid_lower = {ref.lower() for ref in valid_market_refs}
                pred_market = str(existing_pred["market_id"]).strip()
                if pred_market.lower() not in valid_lower:
                    raise BlotterResolutionError(
                        409,
                        (
                            f"Linked prediction market_id '{pred_market}' does not match "
                            f"blotter entry '{clean_ledger_id}' market references {sorted(valid_market_refs)}."
                        ),
                    )

                # Fail closed on inverted-polarity cross-venue pairs so YES/NO cannot flip silently
                pair = self.store.get_cross_venue_pair(entry["pair_id"])
                if pair and pair.get("polarity_inverted"):
                    raise BlotterResolutionError(
                        409,
                        (
                            f"Cross-venue pair '{entry['pair_id']}' has inverted polarity; "
                            "cannot automatically map a single YES/NO settlement to an inverted pair forecast."
                        ),
                    )

                # Timeframe & chronology validation (prevent lookahead or expired-window mismatch)
                pred_ts_raw = existing_pred.get("forecast_timestamp") or existing_pred.get("created_at")
                pred_ts = _try_parse_iso(pred_ts_raw)
                if pred_ts is not None and pred_ts > resolved_dt:
                    raise BlotterResolutionError(
                        400,
                        (
                            f"Linked forecast '{linked_pred_id}' timestamp ({pred_ts_raw}) is in the future "
                            "relative to settlement confirmation."
                        ),
                    )

                pred_tf = str(existing_pred.get("timeframe") or "").strip()
                if expected_tf_clean and pred_tf and pred_tf.lower() != expected_tf_clean.lower():
                    raise BlotterResolutionError(
                        409,
                        (
                            f"Linked forecast timeframe '{pred_tf}' does not match "
                            f"expected timeframe '{expected_tf_clean}'."
                        ),
                    )

                # If forecast timeframe is an explicit ISO cutoff earlier than trade entry, reject mismatch
                pred_tf_dt = _try_parse_iso(pred_tf)
                entry_dt = _try_parse_iso(entry.get("recorded_at"))
                if pred_tf_dt is not None and entry_dt is not None and pred_tf_dt < entry_dt:
                    raise BlotterResolutionError(
                        409,
                        (
                            f"Linked forecast timeframe cutoff ({pred_tf}) expired before "
                            f"blotter trade timestamp ({entry.get('recorded_at')})."
                        ),
                    )

                # Authoritative Arena model attribution strictly from forecast record
                authoritative_agent_id, authoritative_arena_model = (
                    _resolve_authoritative_model_attribution(existing_pred, manual_arena_model)
                )

            # Evaluate outcome strictly relative to forecast's outcome definition (YES=1, NO=0, VOID=None)
            is_test_split = (
                existing_pred is not None
                and str(existing_pred.get("dataset_split") or "").strip().upper() == "TEST"
            )
            outcome_matched: bool | None
            if norm_result == "VOID" or is_test_split:
                # VOID and quarantined TEST-split outcomes must never mutate mistake-training memory.
                outcome_matched = None
            elif existing_pred is not None:
                y_val = 1 if norm_result == "YES" else 0
                pred_p = float(existing_pred["predicted_prob"])
                pred_dir = str(existing_pred.get("predicted_direction") or "").strip().upper()
                if pred_dir in ("YES", "UP"):
                    forecast_expects_yes = True
                elif pred_dir in ("NO", "DOWN"):
                    forecast_expects_yes = False
                else:
                    forecast_expects_yes = pred_p >= 0.5
                outcome_matched = (forecast_expects_yes and y_val == 1) or (
                    (not forecast_expects_yes) and y_val == 0
                )
            else:
                outcome_matched = (
                    entry["outcome_status"] in ("HEDGED_FULL_FILL", "HEDGED_PARTIAL_LEG1")
                    and float(entry["locked_arb_edge_usd"]) >= 0.0
                    and float(entry["realized_pnl_usd"]) >= 0.0
                )

            subject_key = f"paper_blotter:{entry['pair_id'].lower()}"
            expected_outcome: dict[str, Any] = {
                "execution_id": entry["execution_id"],
                "pair_id": entry["pair_id"],
                "outcome_status": entry["outcome_status"],
                "matched_shares": entry["matched_shares"],
                "unwound_shares": entry["unwound_shares"],
                "locked_arb_edge_usd": entry["locked_arb_edge_usd"],
                "simulated_realized_pnl_usd": entry["realized_pnl_usd"],
                "outcome_definition": (
                    "FORECAST_YES_PROPOSITION" if existing_pred is not None else "PAPER_BLOTTER_SETTLEMENT"
                ),
            }
            if existing_pred is not None:
                expected_outcome["linked_prediction_id"] = linked_pred_id
                expected_outcome["forecast_question"] = existing_pred["question"]
                expected_outcome["predicted_prob"] = existing_pred["predicted_prob"]
                expected_outcome["predicted_direction"] = existing_pred.get("predicted_direction")
                expected_outcome["arena_model_id"] = authoritative_arena_model
                if existing_pred.get("timeframe"):
                    expected_outcome["timeframe"] = existing_pred.get("timeframe")
                if existing_pred.get("dataset_split"):
                    expected_outcome["dataset_split"] = existing_pred.get("dataset_split")
                if existing_pred.get("dataset_snapshot_id"):
                    expected_outcome["dataset_snapshot_id"] = existing_pred.get("dataset_snapshot_id")

            evidence_item: dict[str, Any] = {
                "source_type": "PAPER_BLOTTER_RESOLUTION",
                "source_ref_id": clean_ledger_id,
                "execution_id": entry["execution_id"],
                "pair_id": entry["pair_id"],
                "resolution_result": norm_result,
                "resolution_source": source_clean,
                "provenance": "OPERATOR_CONFIRMED",
                "outcome_definition": (
                    "FORECAST_YES_PROPOSITION" if existing_pred is not None else "PAPER_BLOTTER_SETTLEMENT"
                ),
                "scored": norm_result != "VOID",
                "confirmed_by": operator_username,
                "resolved_at": resolved_at,
                "notes": notes_clean,
            }
            if authoritative_arena_model:
                evidence_item["arena_model_id"] = authoritative_arena_model
            if linked_pred_id:
                evidence_item["linked_prediction_id"] = linked_pred_id

            # Stage 1 -> 2 -> 3 of LearningEngine (inside transaction)
            cycle = self.learning_engine.record_observation(
                source_type="PAPER_BLOTTER_RESOLUTION",
                source_ref_id=clean_ledger_id,
                agent_id=authoritative_agent_id,
                subject_key=subject_key,
                hypothesis=(
                    f"Paper execution {entry['execution_id']} on {entry['pair_id']} "
                    f"({entry['outcome_status']}) confirmed against actual market outcome {norm_result}."
                ),
                expected_outcome=expected_outcome,
                evidence=[evidence_item],
            )
            cycle = self.learning_engine.record_evaluation(
                cycle["cycle_id"],
                evaluation_notes=(
                    f"Operator {operator_username} confirmed market outcome={norm_result} "
                    f"(source={source_clean})"
                    + (
                        f" linked to forecast {linked_pred_id} ({authoritative_agent_id})"
                        if linked_pred_id
                        else ""
                    )
                    + "."
                ),
                evidence=[evidence_item],
            )
            cycle = self.learning_engine.record_decision(
                cycle["cycle_id"],
                decision_summary=(
                    f"Record verified outcome {norm_result} for ledger {clean_ledger_id} "
                    "into Learning Core without altering Risk Gate policy."
                ),
            )

            # Resolve linked prediction inside the same transaction (zero partial state on failure)
            resolved_prediction: dict[str, Any] | None = None
            if linked_pred_id:
                mapped_outcome: int | str = (
                    1 if norm_result == "YES" else (0 if norm_result == "NO" else "VOID")
                )
                resolved_prediction = self.prob_engine.resolve_prediction(
                    linked_pred_id,
                    outcome=mapped_outcome,
                    resolution_source=source_clean,
                    reviewer_id=operator_username,
                    notes=notes_clean,
                    provenance="OPERATOR_CONFIRMED",
                    create_error_learning=False,
                    external_learning_cycle_id=cycle["cycle_id"],
                )
                if resolved_prediction.get("brier_score") is not None:
                    evidence_item["brier_score"] = resolved_prediction["brier_score"]
                if resolved_prediction.get("log_loss") is not None:
                    evidence_item["log_loss"] = resolved_prediction["log_loss"]

            actual_outcome_payload: dict[str, Any] = {
                "resolution_result": norm_result,
                "resolution_source": source_clean,
                "provenance": "OPERATOR_CONFIRMED",
                "confirmed_by": operator_username,
                "resolved_at": resolved_at,
                "notes": notes_clean,
                "arena_model_id": authoritative_arena_model,
                "linked_prediction_id": linked_pred_id,
                "scored": norm_result != "VOID",
                "void_excluded_from_training": norm_result == "VOID",
                "test_split_quarantined": is_test_split,
                "dataset_split": existing_pred.get("dataset_split") if existing_pred else None,
            }
            if resolved_prediction is not None:
                actual_outcome_payload["prediction_status"] = resolved_prediction["status"]
                actual_outcome_payload["brier_score"] = resolved_prediction.get("brier_score")
                actual_outcome_payload["log_loss"] = resolved_prediction.get("log_loss")

            # Stage 4 of LearningEngine (VOID passes outcome_matched=None -> never records mistake memory)
            cycle = self.learning_engine.record_outcome(
                cycle["cycle_id"],
                actual_outcome=actual_outcome_payload,
                outcome_matched=outcome_matched,
                evidence=[evidence_item],
            )

            # Final blotter ledger persistence inside the same atomic transaction
            updated_entry = self.store.resolve_paper_pnl_entry(
                clean_ledger_id,
                resolution_result=norm_result,
                resolution_notes=notes_clean,
                resolved_by=operator_username,
                resolved_at=resolved_at,
                learning_cycle_id=cycle["cycle_id"],
                linked_prediction_id=linked_pred_id,
                arena_model_id=authoritative_arena_model,
                resolution_source=source_clean,
                resolution_evidence=[evidence_item],
            )

            summary = self.store.get_paper_summary()

        return {
            "provenance": "SIMULATED",
            "live_trading_enabled": False,
            "entry": updated_entry,
            "summary": summary,
            "learning_cycle": cycle,
            "learning_cycle_id": cycle["cycle_id"],
            "resolved_prediction": resolved_prediction,
        }
