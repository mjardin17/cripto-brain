"""Read-Only Official Prediction-Market Settlement Verification & Operator Review Queue.

Supports read-only official settlement retrieval for:
- Polymarket Gamma API (`closed`, `umaResolutionStatus`, `umaResolutionStatuses`, `outcomes`, `outcomePrices`, `closedTime`)
- Kalshi Trade API v2 (`status`, `result`, `settlement_value_dollars`, `settlement_ts`, `close_time`)

Safety & Correctness Guarantees:
- Strictly read-only public market settlement lookups (never signs or submits trades).
- Never automatically finalizes any prediction or paper blotter entry.
- Explicitly detects and quarantines DISPUTED, CONFLICTING_EVIDENCE, AMBIGUOUS, and
  UNVERIFIED settlements so operators cannot accidentally finalize contested markets.
- Operator approval runs atomically inside `store.transaction()`, delegating to
  `PaperBlotterResolutionService` (when linked to a blotter entry) or
  `ProbabilityIntelligenceEngine.resolve_prediction` (when linked to a prediction).
"""

from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.execution.blotter_resolution import BlotterResolutionError, PaperBlotterResolutionService
from backend.intelligence.probability import ProbabilityIntelligenceEngine, parse_iso_utc
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso
from backend.venues.kalshi import KalshiReadOnlyClient
from backend.venues.polymarket import PolymarketReadOnlyClient

MARKET_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SUPPORTED_SETTLEMENT_VENUES = {"polymarket", "kalshi"}
FINALIZABLE_QUEUE_STATUS = "PENDING_OPERATOR_REVIEW"
BLOCKED_QUEUE_STATUSES = {"DISPUTED", "CONFLICTING_EVIDENCE", "AMBIGUOUS", "UNVERIFIED"}


class SettlementQueueError(Exception):
    """Typed HTTP-mapped error for settlement verification queue operations."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ParsedOfficialSettlement:
    venue: str
    market_id: str
    source_name: str
    proposed_outcome: str  # YES | NO | VOID | UNKNOWN
    verification_status: str  # PENDING_OPERATOR_REVIEW | DISPUTED | CONFLICTING_EVIDENCE | AMBIGUOUS | UNVERIFIED
    settlement_timestamp: str | None
    evidence: dict[str, Any]
    conflict_reasons: list[str] = field(default_factory=list)


def validate_market_identifier(market_id: str) -> str:
    cleaned = (market_id or "").strip()
    if not cleaned or not MARKET_ID_PATTERN.match(cleaned):
        raise SettlementQueueError(
            400,
            "Invalid market_id format: expected 1-128 characters matching [A-Za-z0-9._:-].",
        )
    return cleaned


def _parse_json_list_field(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _normalize_optional_iso(raw: Any) -> str | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Polymarket Gamma closedTime often uses "YYYY-MM-DD HH:MM:SS+00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text + ":00"
    try:
        return parse_iso_utc(text).isoformat()
    except ValueError:
        return text


def parse_polymarket_settlement(market_id: str, raw: dict[str, Any]) -> ParsedOfficialSettlement:
    """Fail-closed parser for official Polymarket Gamma market settlement state."""
    closed = bool(raw.get("closed", False))
    uma_status = str(raw.get("umaResolutionStatus") or "").strip().lower()
    uma_statuses = [str(s).strip().lower() for s in _parse_json_list_field(raw.get("umaResolutionStatuses"))]
    outcomes = [str(o).strip().lower() for o in _parse_json_list_field(raw.get("outcomes"))]
    raw_prices = _parse_json_list_field(raw.get("outcomePrices"))
    settlement_ts = _normalize_optional_iso(
        raw.get("closedTime") or raw.get("umaEndDate") or raw.get("endDate")
    )
    resolution_source = str(raw.get("resolutionSource") or "polymarket_gamma_uma").strip() or "polymarket_gamma_uma"

    evidence = {
        "venue": "polymarket",
        "market_id": str(raw.get("id") or market_id),
        "condition_id": str(raw.get("conditionId") or ""),
        "question": str(raw.get("question") or ""),
        "closed": closed,
        "uma_resolution_status": uma_status,
        "uma_resolution_statuses": uma_statuses,
        "outcomes": outcomes,
        "outcome_prices": raw_prices,
        "resolution_source": resolution_source,
        "automatically_resolved": bool(raw.get("automaticallyResolved", False)),
        "closed_time": settlement_ts,
        "end_date": _normalize_optional_iso(raw.get("endDate")),
    }

    conflicts: list[str] = []

    if uma_status == "disputed" or "disputed" in uma_statuses:
        conflicts.append("Polymarket UMA oracle resolution is currently DISPUTED.")
        return ParsedOfficialSettlement(
            venue="polymarket",
            market_id=market_id,
            source_name=f"polymarket_gamma:{resolution_source[:80]}",
            proposed_outcome="UNKNOWN",
            verification_status="DISPUTED",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    if not closed:
        conflicts.append("Polymarket market is not yet closed.")
        return ParsedOfficialSettlement(
            venue="polymarket",
            market_id=market_id,
            source_name=f"polymarket_gamma:{resolution_source[:80]}",
            proposed_outcome="UNKNOWN",
            verification_status="UNVERIFIED",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    if len(outcomes) != 2 or len(raw_prices) != 2 or "yes" not in outcomes or "no" not in outcomes:
        conflicts.append("Polymarket market does not have a strict binary Yes/No outcome price vector.")
        return ParsedOfficialSettlement(
            venue="polymarket",
            market_id=market_id,
            source_name=f"polymarket_gamma:{resolution_source[:80]}",
            proposed_outcome="UNKNOWN",
            verification_status="AMBIGUOUS",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    try:
        price_map = {outcomes[i]: float(raw_prices[i]) for i in range(2)}
        yes_p = price_map["yes"]
        no_p = price_map["no"]
        if not (math.isfinite(yes_p) and math.isfinite(no_p)):
            raise ValueError("non-finite price")
    except (TypeError, ValueError, KeyError):
        conflicts.append("Unable to parse numeric Polymarket terminal outcomePrices.")
        return ParsedOfficialSettlement(
            venue="polymarket",
            market_id=market_id,
            source_name=f"polymarket_gamma:{resolution_source[:80]}",
            proposed_outcome="UNKNOWN",
            verification_status="AMBIGUOUS",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    if uma_status != "resolved":
        conflicts.append(
            f"Polymarket umaResolutionStatus is '{uma_status or 'missing'}' (expected 'resolved')."
        )
        return ParsedOfficialSettlement(
            venue="polymarket",
            market_id=market_id,
            source_name=f"polymarket_gamma:{resolution_source[:80]}",
            proposed_outcome="UNKNOWN",
            verification_status="UNVERIFIED",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    # Check unambiguous terminal settlement prices
    if yes_p >= 0.999 and no_p <= 0.001:
        return ParsedOfficialSettlement(
            venue="polymarket",
            market_id=market_id,
            source_name=f"polymarket_gamma:{resolution_source[:80]}",
            proposed_outcome="YES",
            verification_status=FINALIZABLE_QUEUE_STATUS,
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=[],
        )
    if no_p >= 0.999 and yes_p <= 0.001:
        return ParsedOfficialSettlement(
            venue="polymarket",
            market_id=market_id,
            source_name=f"polymarket_gamma:{resolution_source[:80]}",
            proposed_outcome="NO",
            verification_status=FINALIZABLE_QUEUE_STATUS,
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=[],
        )

    # Ambiguous split/void/unsettled payout (e.g. 0.5/0.5 or 0/0)
    conflicts.append(
        f"Ambiguous Polymarket terminal outcomePrices (Yes={yes_p}, No={no_p}); manual operator audit required."
    )
    return ParsedOfficialSettlement(
        venue="polymarket",
        market_id=market_id,
        source_name=f"polymarket_gamma:{resolution_source[:80]}",
        proposed_outcome="VOID" if (yes_p == 0.0 and no_p == 0.0) else "UNKNOWN",
        verification_status="AMBIGUOUS",
        settlement_timestamp=settlement_ts,
        evidence=evidence,
        conflict_reasons=conflicts,
    )


def parse_kalshi_settlement(ticker: str, raw: dict[str, Any]) -> ParsedOfficialSettlement:
    """Fail-closed parser for official Kalshi Trade API v2 market settlement state."""
    mkt = raw.get("market") if isinstance(raw.get("market"), dict) else raw
    status = str(mkt.get("status") or "").strip().lower()
    result = str(mkt.get("result") or "").strip().lower()
    settlement_ts = _normalize_optional_iso(
        mkt.get("settlement_ts") or mkt.get("expiration_time") or mkt.get("close_time")
    )

    settlement_val: float | None = None
    raw_val_dollars = mkt.get("settlement_value_dollars")
    raw_val_cents = mkt.get("settlement_value")
    if raw_val_dollars not in (None, ""):
        try:
            v = float(raw_val_dollars)
            if math.isfinite(v):
                settlement_val = v
        except (TypeError, ValueError):
            settlement_val = None
    elif raw_val_cents not in (None, ""):
        try:
            v = float(raw_val_cents)
            if math.isfinite(v):
                settlement_val = v / 100.0 if v > 1.0 else v
        except (TypeError, ValueError):
            settlement_val = None

    evidence = {
        "venue": "kalshi",
        "market_id": str(mkt.get("ticker") or ticker),
        "event_ticker": str(mkt.get("event_ticker") or ""),
        "title": str(mkt.get("title") or ""),
        "status": status,
        "result": result,
        "settlement_value_dollars": settlement_val,
        "settlement_ts": settlement_ts,
        "close_time": _normalize_optional_iso(mkt.get("close_time")),
        "expiration_time": _normalize_optional_iso(mkt.get("expiration_time")),
        "rules_primary": str(mkt.get("rules_primary") or "")[:400],
    }

    conflicts: list[str] = []

    if status == "disputed":
        conflicts.append("Kalshi market settlement status is DISPUTED.")
        return ParsedOfficialSettlement(
            venue="kalshi",
            market_id=ticker,
            source_name="kalshi_trade_api_v2",
            proposed_outcome="UNKNOWN",
            verification_status="DISPUTED",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    if status not in ("finalized", "settled"):
        conflicts.append(f"Kalshi market status is '{status or 'missing'}' (not finalized/settled).")
        return ParsedOfficialSettlement(
            venue="kalshi",
            market_id=ticker,
            source_name="kalshi_trade_api_v2",
            proposed_outcome="UNKNOWN",
            verification_status="UNVERIFIED",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    if result == "yes":
        if settlement_val is not None and settlement_val < 0.999:
            conflicts.append(
                f"Conflicting Kalshi settlement evidence: result='yes' contradicts settlement_value_dollars={settlement_val}."
            )
            return ParsedOfficialSettlement(
                venue="kalshi",
                market_id=ticker,
                source_name="kalshi_trade_api_v2",
                proposed_outcome="UNKNOWN",
                verification_status="CONFLICTING_EVIDENCE",
                settlement_timestamp=settlement_ts,
                evidence=evidence,
                conflict_reasons=conflicts,
            )
        return ParsedOfficialSettlement(
            venue="kalshi",
            market_id=ticker,
            source_name="kalshi_trade_api_v2",
            proposed_outcome="YES",
            verification_status=FINALIZABLE_QUEUE_STATUS,
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=[],
        )

    if result == "no":
        if settlement_val is not None and settlement_val > 0.001:
            conflicts.append(
                f"Conflicting Kalshi settlement evidence: result='no' contradicts settlement_value_dollars={settlement_val}."
            )
            return ParsedOfficialSettlement(
                venue="kalshi",
                market_id=ticker,
                source_name="kalshi_trade_api_v2",
                proposed_outcome="UNKNOWN",
                verification_status="CONFLICTING_EVIDENCE",
                settlement_timestamp=settlement_ts,
                evidence=evidence,
                conflict_reasons=conflicts,
            )
        return ParsedOfficialSettlement(
            venue="kalshi",
            market_id=ticker,
            source_name="kalshi_trade_api_v2",
            proposed_outcome="NO",
            verification_status=FINALIZABLE_QUEUE_STATUS,
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=[],
        )

    if result in ("void", "all_no", "all_yes"):
        if result == "void":
            return ParsedOfficialSettlement(
                venue="kalshi",
                market_id=ticker,
                source_name="kalshi_trade_api_v2",
                proposed_outcome="VOID",
                verification_status=FINALIZABLE_QUEUE_STATUS,
                settlement_timestamp=settlement_ts,
                evidence=evidence,
                conflict_reasons=[],
            )
        conflicts.append(f"Non-standard Kalshi multi-leg settlement result '{result}' requires manual review.")
        return ParsedOfficialSettlement(
            venue="kalshi",
            market_id=ticker,
            source_name="kalshi_trade_api_v2",
            proposed_outcome="UNKNOWN",
            verification_status="AMBIGUOUS",
            settlement_timestamp=settlement_ts,
            evidence=evidence,
            conflict_reasons=conflicts,
        )

    conflicts.append(f"Kalshi market finalized with unsupported or missing result '{result or 'empty'}'.")
    return ParsedOfficialSettlement(
        venue="kalshi",
        market_id=ticker,
        source_name="kalshi_trade_api_v2",
        proposed_outcome="UNKNOWN",
        verification_status="AMBIGUOUS",
        settlement_timestamp=settlement_ts,
        evidence=evidence,
        conflict_reasons=conflicts,
    )


class SettlementVerificationService:
    """Coordinates read-only official settlement lookups and fail-closed operator review."""

    def __init__(
        self,
        *,
        store: DuckDBStore,
        poly_client: PolymarketReadOnlyClient,
        kalshi_client: KalshiReadOnlyClient,
        prob_engine: ProbabilityIntelligenceEngine,
        blotter_resolution_service: PaperBlotterResolutionService,
    ) -> None:
        self.store = store
        self.poly_client = poly_client
        self.kalshi_client = kalshi_client
        self.prob_engine = prob_engine
        self.blotter_resolution_service = blotter_resolution_service

    async def _fetch_single_venue(self, venue: str, market_id: str) -> ParsedOfficialSettlement:
        norm_venue = (venue or "").strip().lower()
        clean_id = validate_market_identifier(market_id)
        if norm_venue == "polymarket":
            raw = await self.poly_client.fetch_market_raw(clean_id)
            if raw is None:
                return ParsedOfficialSettlement(
                    venue="polymarket",
                    market_id=clean_id,
                    source_name="polymarket_gamma",
                    proposed_outcome="UNKNOWN",
                    verification_status="UNVERIFIED",
                    settlement_timestamp=None,
                    evidence={"venue": "polymarket", "market_id": clean_id, "found": False},
                    conflict_reasons=[f"Polymarket market '{clean_id}' was not found on Gamma API."],
                )
            return parse_polymarket_settlement(clean_id, raw)
        if norm_venue == "kalshi":
            raw = await self.kalshi_client.fetch_market_raw(clean_id)
            if raw is None:
                return ParsedOfficialSettlement(
                    venue="kalshi",
                    market_id=clean_id,
                    source_name="kalshi_trade_api_v2",
                    proposed_outcome="UNKNOWN",
                    verification_status="UNVERIFIED",
                    settlement_timestamp=None,
                    evidence={"venue": "kalshi", "market_id": clean_id, "found": False},
                    conflict_reasons=[f"Kalshi market '{clean_id}' was not found on Trade API v2."],
                )
            return parse_kalshi_settlement(clean_id, raw)
        raise SettlementQueueError(
            400,
            f"Unsupported settlement venue '{venue}'. Supported: {sorted(SUPPORTED_SETTLEMENT_VENUES)}",
        )

    async def fetch_and_enqueue(
        self,
        *,
        venue: str,
        market_id: str,
        fetched_by: str,
        linked_prediction_id: str | None = None,
        linked_ledger_id: str | None = None,
        secondary_venue: str | None = None,
        secondary_market_id: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve official read-only settlement state and place into operator review queue.

        Never automatically finalizes predictions or paper blotter entries.
        If a secondary venue/market is supplied (e.g. cross-venue pair), cross-checks
        both venues and flags CONFLICTING_EVIDENCE whenever they disagree.
        """
        norm_venue = (venue or "").strip().lower()
        if norm_venue not in SUPPORTED_SETTLEMENT_VENUES:
            raise SettlementQueueError(
                400,
                f"Unsupported settlement venue '{venue}'. Supported: {sorted(SUPPORTED_SETTLEMENT_VENUES)}",
            )
        clean_id = validate_market_identifier(market_id)
        pred_id = (linked_prediction_id or "").strip() or None
        ledger_id = (linked_ledger_id or "").strip() or None

        if pred_id is not None:
            pred = self.prob_engine.get_prediction(pred_id)
            if not pred:
                raise SettlementQueueError(404, f"Unknown linked_prediction_id '{pred_id}'.")
            if pred["status"] != "OPEN":
                raise SettlementQueueError(
                    409,
                    f"Linked prediction '{pred_id}' is already finalized with status '{pred['status']}'.",
                )
        if ledger_id is not None:
            entry = self.store.get_paper_pnl_entry(ledger_id)
            if not entry:
                raise SettlementQueueError(404, f"Unknown linked_ledger_id '{ledger_id}'.")
            if entry["resolution_result"] != "PENDING":
                raise SettlementQueueError(
                    409,
                    f"Linked blotter entry '{ledger_id}' is already confirmed as '{entry['resolution_result']}'.",
                )

        try:
            primary = await self._fetch_single_venue(norm_venue, clean_id)
        except SettlementQueueError:
            raise
        except Exception as exc:
            primary = ParsedOfficialSettlement(
                venue=norm_venue,
                market_id=clean_id,
                source_name=f"{norm_venue}_read_only_adapter",
                proposed_outcome="UNKNOWN",
                verification_status="UNVERIFIED",
                settlement_timestamp=None,
                evidence={"venue": norm_venue, "market_id": clean_id, "fetch_error": type(exc).__name__},
                conflict_reasons=[f"Official settlement lookup failed ({type(exc).__name__}); queued as UNVERIFIED."],
            )

        evidence: dict[str, Any] = dict(primary.evidence)
        conflicts = list(primary.conflict_reasons)
        status = primary.verification_status
        proposed = primary.proposed_outcome
        source_name = primary.source_name

        # Optional cross-venue corroboration check (detects cross-venue disagreement fail-closed)
        if secondary_venue and secondary_market_id:
            sec_venue = secondary_venue.strip().lower()
            sec_id = validate_market_identifier(secondary_market_id)
            try:
                secondary = await self._fetch_single_venue(sec_venue, sec_id)
            except Exception as exc:
                secondary = ParsedOfficialSettlement(
                    venue=sec_venue,
                    market_id=sec_id,
                    source_name=f"{sec_venue}_read_only_adapter",
                    proposed_outcome="UNKNOWN",
                    verification_status="UNVERIFIED",
                    settlement_timestamp=None,
                    evidence={"venue": sec_venue, "market_id": sec_id, "fetch_error": type(exc).__name__},
                    conflict_reasons=[f"Secondary venue lookup failed ({type(exc).__name__})."],
                )
            evidence["secondary_venue_evidence"] = secondary.evidence
            source_name = f"{source_name}+{secondary.source_name}"[:150]
            if secondary.verification_status in BLOCKED_QUEUE_STATUSES:
                conflicts.extend(secondary.conflict_reasons)
                if status == FINALIZABLE_QUEUE_STATUS:
                    status = secondary.verification_status
            elif (
                status == FINALIZABLE_QUEUE_STATUS
                and secondary.verification_status == FINALIZABLE_QUEUE_STATUS
                and proposed != secondary.proposed_outcome
            ):
                conflicts.append(
                    f"Conflicting cross-venue settlement evidence: {norm_venue} reported "
                    f"'{proposed}' while {sec_venue} reported '{secondary.proposed_outcome}'."
                )
                status = "CONFLICTING_EVIDENCE"
                proposed = "UNKNOWN"

        queue_id = f"SETQ-{uuid.uuid4().hex[:10].upper()}"
        queued = self.store.insert_settlement_queue_item(
            {
                "queue_id": queue_id,
                "venue": norm_venue,
                "market_id": clean_id,
                "linked_prediction_id": pred_id,
                "linked_ledger_id": ledger_id,
                "source_name": source_name,
                "proposed_outcome": proposed,
                "verification_status": status,
                "settlement_timestamp": primary.settlement_timestamp,
                "evidence": evidence,
                "conflict_reasons": conflicts,
                "fetched_by": fetched_by.strip()[:64],
                "fetched_at": utc_now_iso(),
                "review_notes": "",
            }
        )
        return queued

    def list_queue(
        self,
        *,
        verification_status: str | None = None,
        venue: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        items = self.store.list_settlement_queue(
            verification_status=verification_status,
            venue=venue,
            limit=limit,
        )
        counts: dict[str, int] = {}
        for item in items:
            st = str(item.get("verification_status") or "UNKNOWN")
            counts[st] = counts.get(st, 0) + 1
        return {
            "provenance": "LIVE",
            "live_trading_enabled": False,
            "auto_finalize_enabled": False,
            "count": len(items),
            "status_counts": counts,
            "items": items,
        }

    def review_queue_item(
        self,
        *,
        queue_id: str,
        action: str,
        operator_username: str,
        operator_confirmed: bool = False,
        notes: str = "",
        linked_prediction_id: str | None = None,
        linked_ledger_id: str | None = None,
    ) -> dict[str, Any]:
        """Operator review gate: APPROVE (strictly on verified non-conflicting items), REJECT, or MARK_DISPUTED."""
        clean_qid = (queue_id or "").strip()
        norm_action = (action or "").strip().upper()
        if norm_action not in ("APPROVE", "REJECT", "MARK_DISPUTED"):
            raise SettlementQueueError(422, "action must be one of APPROVE, REJECT, or MARK_DISPUTED.")

        notes_clean = (notes or "").strip()[:600]
        now_iso = utc_now_iso()

        with self.store.transaction():
            item = self.store.get_settlement_queue_item(clean_qid)
            if not item:
                raise SettlementQueueError(404, f"Unknown settlement queue_id '{clean_qid}'.")

            cur_status = str(item["verification_status"]).upper()
            if cur_status in ("APPROVED_AND_FINALIZED", "REJECTED"):
                raise SettlementQueueError(
                    409,
                    f"Settlement queue item '{clean_qid}' is already finalized with status '{cur_status}'.",
                )

            if norm_action == "REJECT":
                updated = self.store.update_settlement_queue_review(
                    clean_qid,
                    verification_status="REJECTED",
                    reviewed_by=operator_username,
                    reviewed_at=now_iso,
                    review_notes=notes_clean or "Rejected by operator review.",
                )
                return {
                    "provenance": "OPERATOR_CONFIRMED",
                    "live_trading_enabled": False,
                    "queue_item": updated,
                    "blotter_resolution": None,
                    "resolved_prediction": None,
                }

            if norm_action == "MARK_DISPUTED":
                reasons = list(item.get("conflict_reasons") or [])
                reasons.append(
                    f"Operator {operator_username} flagged settlement as DISPUTED: "
                    f"{notes_clean or 'manual dispute flag'}"
                )
                updated = self.store.update_settlement_queue_review(
                    clean_qid,
                    verification_status="DISPUTED",
                    reviewed_by=operator_username,
                    reviewed_at=now_iso,
                    review_notes=notes_clean or "Marked disputed by operator.",
                    conflict_reasons=reasons,
                )
                return {
                    "provenance": "OPERATOR_CONFIRMED",
                    "live_trading_enabled": False,
                    "queue_item": updated,
                    "blotter_resolution": None,
                    "resolved_prediction": None,
                }

            # APPROVE path: strict fail-closed checks
            if not operator_confirmed:
                raise SettlementQueueError(
                    400,
                    "Explicit operator_confirmed=true is required to finalize a verified settlement.",
                )
            if cur_status != FINALIZABLE_QUEUE_STATUS or item.get("conflict_reasons"):
                raise SettlementQueueError(
                    409,
                    (
                        f"Cannot finalize settlement queue item '{clean_qid}' with status '{cur_status}': "
                        "disputed, conflicting, ambiguous, or unverified settlements are blocked from finalization."
                    ),
                )
            proposed = str(item["proposed_outcome"]).upper()
            if proposed not in ("YES", "NO", "VOID"):
                raise SettlementQueueError(
                    409,
                    f"Cannot finalize settlement queue item '{clean_qid}' with proposed_outcome '{proposed}'.",
                )

            eff_ledger_id = (linked_ledger_id or "").strip() or item.get("linked_ledger_id")
            eff_pred_id = (linked_prediction_id or "").strip() or item.get("linked_prediction_id")
            if not eff_ledger_id and not eff_pred_id:
                raise SettlementQueueError(
                    400,
                    "Approving a settlement queue item requires linked_ledger_id or linked_prediction_id.",
                )

            source_label = f"{item['source_name']}:{clean_qid}"
            combined_notes = (
                f"Verified via {item['source_name']} ({clean_qid})"
                + (f" at {item['settlement_timestamp']}" if item.get("settlement_timestamp") else "")
                + (f". {notes_clean}" if notes_clean else ".")
            )[:600]

            blotter_res: dict[str, Any] | None = None
            resolved_pred: dict[str, Any] | None = None
            cycle_id: str | None = None

            if eff_ledger_id:
                try:
                    blotter_res = self.blotter_resolution_service.resolve_entry(
                        ledger_id=str(eff_ledger_id),
                        resolution_result=proposed,
                        operator_username=operator_username,
                        notes=combined_notes,
                        resolution_source=source_label,
                        linked_prediction_id=str(eff_pred_id) if eff_pred_id else None,
                    )
                except BlotterResolutionError as exc:
                    raise SettlementQueueError(exc.status_code, exc.detail) from exc
                cycle_id = blotter_res.get("learning_cycle_id")
                resolved_pred = blotter_res.get("resolved_prediction")
            elif eff_pred_id:
                mapped: int | str = 1 if proposed == "YES" else (0 if proposed == "NO" else "VOID")
                try:
                    resolved_pred = self.prob_engine.resolve_prediction(
                        str(eff_pred_id),
                        outcome=mapped,
                        resolution_source=source_label,
                        reviewer_id=operator_username,
                        notes=combined_notes,
                        provenance="OFFICIAL_SETTLEMENT_OPERATOR_APPROVED",
                    )
                except ValueError as exc:
                    msg = str(exc)
                    code = 404 if "Unknown prediction_id" in msg else 409
                    raise SettlementQueueError(code, msg) from exc
                cycle_id = resolved_pred.get("learning_cycle_id")

            updated = self.store.update_settlement_queue_review(
                clean_qid,
                verification_status="APPROVED_AND_FINALIZED",
                reviewed_by=operator_username,
                reviewed_at=now_iso,
                review_notes=combined_notes,
                resolution_cycle_id=cycle_id,
            )

        return {
            "provenance": "OPERATOR_CONFIRMED",
            "live_trading_enabled": False,
            "queue_item": updated,
            "blotter_resolution": blotter_res,
            "resolved_prediction": resolved_pred,
        }
