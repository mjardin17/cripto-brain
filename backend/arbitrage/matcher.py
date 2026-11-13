"""Strict fail-closed cross-venue contract and settlement equivalence verifier."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from backend.venues.kalshi import KalshiBinaryMarket
from backend.venues.polymarket import PolymarketBinaryMarket


@dataclass(frozen=True)
class SettlementMatchAssessment:
    identical_settlement_verified: bool
    status: Literal["VERIFIED", "UNVERIFIED_FAIL_CLOSED", "MISMATCH_BLOCKED"]
    cutoff_delta_hours: float | None
    mismatch_reasons: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]


def _parse_iso_dt(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        cleaned = iso_str.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def _tokenize_title(text: str) -> set[str]:
    stop = {
        "will", "the", "be", "in", "on", "of", "to", "a", "an", "by", "or",
        "and", "for", "before", "after", "at", "is", "are", "yes", "no",
    }
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def title_similarity_score(poly_question: str, kalshi_title: str) -> float:
    a = _tokenize_title(poly_question)
    b = _tokenize_title(kalshi_title)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def assess_contract_equivalence(
    *,
    poly_market: PolymarketBinaryMarket,
    kalshi_market: KalshiBinaryMarket,
    resolution_source_verified: bool = False,
    predicate_verified: bool = False,
    polarity_inverted: bool = False,
    void_rules_compatible: bool = False,
    allow_early_close_variance: bool = False,
    max_cutoff_delta_hours: float = 24.0,
) -> SettlementMatchAssessment:
    """Deterministically verify whether a Polymarket and Kalshi market share identical settlement.

    Fail-closed invariant:
    - Automated text similarity NEVER grants settlement equivalence.
    - Both structural metadata invariants (not negRiskOther, cutoff alignment,
      early-close parity) AND explicit resolution/predicate/void checks must pass.
    """
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    # 1. Binary & non-catchall market structure
    poly_binary_ok = bool(poly_market.condition_id) and not poly_market.neg_risk_other and poly_market.identical_settlement_verified
    checks.append({
        "check_id": "POLY_BINARY_NON_CATCHALL",
        "passed": poly_binary_ok,
        "detail": "Polymarket binary conditionId valid and not negRiskOther"
        if poly_binary_ok
        else "Polymarket conditionId missing or flagged negRiskOther catch-all",
    })
    if not poly_binary_ok:
        reasons.append("Polymarket market failed binary non-catchall structure check")

    # 2. Polarity check (standard YES==YES required unless explicitly mapped)
    polarity_ok = not polarity_inverted
    checks.append({
        "check_id": "OUTCOME_POLARITY_MATCH",
        "passed": polarity_ok,
        "detail": "Direct YES<->YES / NO<->NO outcome polarity"
        if polarity_ok
        else "Inverted outcome polarity requires unverified manual remapping (blocked)",
    })
    if not polarity_ok:
        reasons.append("Inverted outcome polarity blocked fail-closed")

    # 3. Cutoff / expiration timestamp alignment
    poly_dt = _parse_iso_dt(poly_market.end_date)
    kalshi_dt = _parse_iso_dt(kalshi_market.expiration_time or kalshi_market.close_time)
    cutoff_delta_hours: float | None = None
    if poly_dt is None or kalshi_dt is None:
        cutoff_ok = False
        cutoff_detail = "Missing expiration/endDate timestamp on one or both venues"
        reasons.append(cutoff_detail)
    else:
        cutoff_delta_hours = round(abs((poly_dt - kalshi_dt).total_seconds()) / 3600.0, 2)
        cutoff_ok = cutoff_delta_hours <= max_cutoff_delta_hours
        cutoff_detail = (
            f"Expiration delta {cutoff_delta_hours:.2f}h <= {max_cutoff_delta_hours:.1f}h"
            if cutoff_ok
            else f"Expiration delta {cutoff_delta_hours:.2f}h exceeds {max_cutoff_delta_hours:.1f}h tolerance"
        )
        if not cutoff_ok:
            reasons.append(cutoff_detail)
    checks.append({
        "check_id": "EXPIRATION_CUTOFF_ALIGNMENT",
        "passed": cutoff_ok,
        "detail": cutoff_detail,
    })

    # 4. Early close / settlement timer parity
    early_close_ok = (not kalshi_market.can_close_early) or allow_early_close_variance
    checks.append({
        "check_id": "EARLY_CLOSE_PARITY",
        "passed": early_close_ok,
        "detail": "Early close disabled or explicitly reconciled"
        if early_close_ok
        else "Kalshi can_close_early=True creates asymmetric settlement window",
    })
    if not early_close_ok:
        reasons.append("Kalshi can_close_early=True without reconciled parity")

    # 5. Identical predicate & authoritative resolution source
    checks.append({
        "check_id": "IDENTICAL_EVENT_PREDICATE",
        "passed": predicate_verified,
        "detail": "Event definition and strike/threshold verified identical"
        if predicate_verified
        else "Event predicate not verified equivalent (fail-closed)",
    })
    if not predicate_verified:
        reasons.append("Event predicate not attested identical")

    checks.append({
        "check_id": "RESOLUTION_SOURCE_PARITY",
        "passed": resolution_source_verified,
        "detail": "Authoritative settlement oracle/source verified identical"
        if resolution_source_verified
        else "Authoritative settlement source not verified identical (fail-closed)",
    })
    if not resolution_source_verified:
        reasons.append("Resolution source parity not verified")

    checks.append({
        "check_id": "VOID_AND_PUSH_SEMANTICS",
        "passed": void_rules_compatible,
        "detail": "Void, cancellation, and push rules verified compatible"
        if void_rules_compatible
        else "Void/cancellation rules not verified compatible (fail-closed)",
    })
    if not void_rules_compatible:
        reasons.append("Void/cancellation semantics not verified compatible")

    verified = all(c["passed"] for c in checks)
    if verified:
        status: Literal["VERIFIED", "UNVERIFIED_FAIL_CLOSED", "MISMATCH_BLOCKED"] = "VERIFIED"
    elif (not poly_binary_ok) or (not cutoff_ok) or (not polarity_ok) or (not early_close_ok):
        status = "MISMATCH_BLOCKED"
    else:
        status = "UNVERIFIED_FAIL_CLOSED"

    return SettlementMatchAssessment(
        identical_settlement_verified=verified,
        status=status,
        cutoff_delta_hours=cutoff_delta_hours,
        mismatch_reasons=tuple(reasons),
        checks=tuple(checks),
    )
