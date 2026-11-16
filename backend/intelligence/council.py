"""Cross-domain council teams: STOCKS and CRYPTO deliberation groups.

A council is a small team of deterministic personas that deliberate in three
rounds over the app's own persisted market data:

1. OPENING — every member states an independent stance from shared facts.
2. CROSS_EXAMINATION — members see each other's stances and may revise
   (a Risk Sentinel NO vote downgrades every GO to HOLD: fail-closed).
3. VERDICT — weighted majority with a hard rule: the council can only advise
   GO when at least one Risk-Gate-approved opportunity exists in its domain.

The councils are deterministic and free: no LLM calls, no external APIs, no
paid services. They never authorize trades — their output is advisory and
the Risk Gate remains the only execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

COUNCIL_DOMAINS = ("STOCKS", "CRYPTO")

MIN_APPROVED_NET_EDGE_PCT = 2.0
STALE_DATA_MS = 15_000
WEAK_CALIBRATION_BRIER = 0.35

COUNCIL_MEMBERS: tuple[dict[str, str], ...] = (
    {
        "member_id": "edge_analyst",
        "name": "Edge Analyst",
        "role": "YIELD",
        "description": "Hunts approved net edge in the domain and only backs GO above the Risk Gate minimum.",
    },
    {
        "member_id": "risk_sentinel",
        "name": "Risk Sentinel",
        "role": "SAFETY",
        "description": "Holds the veto. Blocks when the kill switch is on, data is stale, or settlement verification is weak.",
    },
    {
        "member_id": "calibration_auditor",
        "name": "Calibration Auditor",
        "role": "QUALITY",
        "description": "Checks resolved prediction Brier scores in the domain; abstains when there is no calibration history.",
    },
    {
        "member_id": "momentum_scout",
        "name": "Momentum Scout",
        "role": "TREND",
        "description": "Compares the latest domain scan against the previous one and follows improvement.",
    },
    {
        "member_id": "pattern_analyst",
        "name": "Pattern Analyst",
        "role": "HISTORY",
        "description": (
            "Reads recorded historical event reactions (disasters, shocks, outages) and backs GO "
            "only when a strong analog base rate exists for the domain."
        ),
    },
)

DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "STOCKS": (
        "stock", "stocks", "share", "shares", "nasdaq", "nyse", "earnings",
        "s&p", "dow jones", "ipo", "equity", "equities", "company",
        "companies", "ceo", "market cap", "fed ", "fomc", "rate cut", "rate hike",
    ),
    "CRYPTO": (
        "btc", "bitcoin", "eth", "ethereum", "solana", "xrp", "doge",
        "crypto", "token", "coin", "blockchain", "stablecoin", "altcoin",
        "binance", "etf" ,
    ),
}

DOMAIN_LABELS: dict[str, str] = {
    "STOCKS": "Stocks Council",
    "CRYPTO": "Crypto Council",
}


@dataclass(frozen=True)
class CouncilMemberDef:
    member_id: str
    name: str
    role: str
    description: str


def get_council_definition(domain: str) -> dict[str, Any]:
    d = domain.strip().upper()
    if d not in COUNCIL_DOMAINS:
        raise ValueError(f"Unknown council domain '{domain}'. Valid: {COUNCIL_DOMAINS}")
    return {
        "domain": d,
        "label": DOMAIN_LABELS[d],
        "keywords": list(DOMAIN_KEYWORDS[d]),
        "members": [dict(m) for m in COUNCIL_MEMBERS],
        "min_approved_net_edge_pct": MIN_APPROVED_NET_EDGE_PCT,
        "advisory_only": True,
        "live_trading_enabled": False,
    }


def _matches_domain(text: str, domain: str) -> bool:
    hay = " " + str(text or "").lower() + " "
    return any(k in hay for k in DOMAIN_KEYWORDS[domain])


def gather_domain_facts(store: DuckDBStore, domain: str) -> dict[str, Any]:
    """Collect the domain's own data: opportunities, pairs, calibration, safety state."""
    d = domain.strip().upper()
    if d not in COUNCIL_DOMAINS:
        raise ValueError(f"Unknown council domain '{domain}'")

    opps = [
        o
        for o in store.list_opportunities(limit=300)
        if _matches_domain(o.get("question") or "", d)
    ]
    cross_opps = [
        o
        for o in store.list_cross_venue_opportunities(limit=300)
        if _matches_domain(o.get("question") or "", d)
    ]
    pairs = [
        p
        for p in store.list_cross_venue_pairs(limit=200)
        if _matches_domain(p.get("poly_question") or "", d)
    ]

    approved = [o for o in opps + cross_opps if o.get("risk_gate_approved")]
    best_net = max(
        (o.get("net_edge_pct") for o in opps + cross_opps if o.get("net_edge_pct") is not None),
        default=None,
    )
    stale = [
        o
        for o in opps + cross_opps
        if int(o.get("data_age_ms") or 0) > STALE_DATA_MS
    ]

    brier_scores: list[float] = []
    with store._lock:
        rows = store._conn.execute(
            """
            SELECT question, subject_key, brier_score FROM predictions
            WHERE status = 'RESOLVED' AND brier_score IS NOT NULL
            ORDER BY created_at DESC LIMIT 300
            """
        ).fetchall()
    for q, subject, brier in rows:
        if _matches_domain(q or "", d) or _matches_domain(subject or "", d):
            try:
                brier_scores.append(float(brier))
            except (TypeError, ValueError):
                continue

    kill_switch = store.is_kill_switch_active()
    verified_pairs = [p for p in pairs if p.get("identical_settlement_verified")]

    # Pattern intelligence: historical event analogs recorded for this domain.
    reaction_counts: dict[str, dict[str, int]] = {}
    for r in store.list_event_reactions(domain=d, limit=200):
        et = str(r.get("event_type") or "UNKNOWN")
        agg = reaction_counts.setdefault(et, {"up": 0, "down": 0, "flat": 0})
        dir_key = str(r.get("reaction_direction") or "FLAT").lower()
        if dir_key in agg:
            agg[dir_key] += 1
    best_pattern: dict[str, Any] | None = None
    for et, agg in reaction_counts.items():
        n = agg["up"] + agg["down"] + agg["flat"]
        if n < 3:
            continue
        dominant = max(agg, key=agg.get)
        base_rate = agg[dominant] / n
        if best_pattern is None or base_rate > best_pattern["base_rate"]:
            best_pattern = {
                "event_type": et,
                "analog_count": n,
                "dominant_direction": dominant.upper(),
                "base_rate": round(base_rate, 4),
            }

    # Next-fluctuation forecast for the strongest pattern driver.
    next_fluctuation: dict[str, Any] | None = None
    if best_pattern is not None:
        from backend.intelligence.fluctuation_analyzer import forecast_next_fluctuations

        try:
            fc = forecast_next_fluctuations(store, domain=d, horizon_days=30.0)
            for f in fc.get("forecasts", []):
                if (
                    f.get("event_type") == best_pattern["event_type"]
                    and not f.get("insufficient_history", True)
                ):
                    next_fluctuation = {
                        "event_type": f["event_type"],
                        "next_expected_at": f.get("next_expected_at"),
                        "days_until_expected": f.get("days_until_expected"),
                        "expected_direction": f.get("expected_direction"),
                        "probability_within_horizon": f.get("probability_within_horizon"),
                        "confidence": f.get("confidence"),
                    }
                    break
        except ValueError:
            next_fluctuation = None

    return {
        "domain": d,
        "opportunities_total": len(opps) + len(cross_opps),
        "approved_count": len(approved),
        "best_net_edge_pct": best_net,
        "approved_opportunities": approved,
        "stale_count": len(stale),
        "pairs_registered": len(pairs),
        "pairs_verified": len(verified_pairs),
        "kill_switch_active": kill_switch,
        "resolved_prediction_samples": len(brier_scores),
        "avg_brier": round(sum(brier_scores) / len(brier_scores), 6) if brier_scores else None,
        "best_pattern_analog": best_pattern,
        "next_fluctuation": next_fluctuation,
    }


def _member_opening(member: dict[str, str], facts: dict[str, Any]) -> dict[str, Any]:
    mid = member["member_id"]
    stance = "HOLD"
    rationale = ""
    metrics: dict[str, Any] = {}

    if mid == "edge_analyst":
        best = facts["best_net_edge_pct"]
        approved = facts["approved_count"]
        metrics = {"approved_count": approved, "best_net_edge_pct": best}
        if approved > 0 and best is not None and best >= MIN_APPROVED_NET_EDGE_PCT:
            stance = "GO"
            rationale = (
                f"{approved} Risk-Gate-approved opportunit{'y' if approved == 1 else 'ies'} in the domain "
                f"with a best net edge of {best}% — above the {MIN_APPROVED_NET_EDGE_PCT}% gate."
            )
        else:
            rationale = (
                "No approved opportunity above the "
                f"{MIN_APPROVED_NET_EDGE_PCT}% net edge gate — nothing worth advising."
            )

    elif mid == "risk_sentinel":
        metrics = {
            "kill_switch_active": facts["kill_switch_active"],
            "stale_count": facts["stale_count"],
            "pairs_verified": facts["pairs_verified"],
            "pairs_registered": facts["pairs_registered"],
        }
        if facts["kill_switch_active"]:
            stance = "NO"
            rationale = "Kill switch is active — all deliberation is moot until it is cleared."
        elif facts["stale_count"] > 0:
            stance = "NO"
            rationale = f"{facts['stale_count']} opportunit{'y' if facts['stale_count'] == 1 else 'ies'} carry stale order-book data beyond {STALE_DATA_MS}ms."
        elif facts["pairs_registered"] > 0 and facts["pairs_verified"] == 0:
            stance = "NO"
            rationale = "Domain pairs exist but none have verified settlement equivalence — fail-closed by design."
        elif facts["opportunities_total"] == 0:
            stance = "HOLD"
            rationale = "No domain data to deliberate on."
        else:
            stance = "GO"
            rationale = "Data fresh, kill switch off, and settlement verification intact."

    elif mid == "calibration_auditor":
        avg = facts["avg_brier"]
        metrics = {"avg_brier": avg, "samples": facts["resolved_prediction_samples"]}
        if avg is None:
            stance = "ABSTAIN"
            rationale = "No resolved predictions for this domain — abstaining rather than guessing."
        elif avg > WEAK_CALIBRATION_BRIER:
            stance = "NO"
            rationale = f"Average Brier {avg} exceeds {WEAK_CALIBRATION_BRIER} — calibration too weak to trust implied edges."
        else:
            stance = "GO"
            rationale = f"Average Brier {avg} is within the acceptable band."

    elif mid == "momentum_scout":
        best = facts["best_net_edge_pct"]
        metrics = {"best_net_edge_pct": best}
        if best is None:
            stance = "HOLD"
            rationale = "No recent scans in this domain to compare."
        elif best > 0:
            stance = "GO"
            rationale = f"Latest domain edge is positive ({best}%)."
        else:
            stance = "HOLD"
            rationale = f"Latest domain edge is non-positive ({best}%)."

    elif mid == "pattern_analyst":
        pattern = facts.get("best_pattern_analog")
        forecast = facts.get("next_fluctuation")
        metrics = {"best_pattern_analog": pattern, "next_fluctuation": forecast}
        if pattern is None:
            stance = "ABSTAIN"
            rationale = "No recorded historical event analogs for this domain — abstaining rather than guessing."
        elif pattern["base_rate"] >= 0.75 and pattern["analog_count"] >= 3:
            stance = "GO"
            rationale = (
                f"{pattern['analog_count']} recorded {pattern['event_type']} analogs moved this domain "
                f"{pattern['dominant_direction']} {pattern['base_rate']:.0%} of the time — a strong historical pattern."
            )
            if forecast:
                rationale += (
                    f" Timing model expects the next {forecast['event_type']} fluctuation around "
                    f"{str(forecast.get('next_expected_at'))[:10]} "
                    f"({forecast.get('probability_within_horizon'):.0%} within 30 days)."
                )
        else:
            stance = "HOLD"
            rationale = (
                f"Recorded pattern base rate {pattern['base_rate']:.0%} over {pattern['analog_count']} analogs "
                "is too weak to lean on."
            )

    return {
        "member_id": mid,
        "name": member["name"],
        "role": member["role"],
        "stance": stance,
        "rationale": rationale,
        "key_metrics": metrics,
    }


def _cross_examine(openings: list[dict[str, Any]], facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Round 2: members revise after seeing each other's stances. Fail-closed."""
    sentinel = next(m for m in openings if m["member_id"] == "risk_sentinel")
    auditor = next(m for m in openings if m["member_id"] == "calibration_auditor")
    revised: list[dict[str, Any]] = []
    for m in openings:
        out = dict(m)
        if m["stance"] == "GO" and sentinel["stance"] == "NO":
            out["stance"] = "HOLD"
            out["rationale"] = f"{m['rationale']} Revised: deferring to Risk Sentinel's veto."
        elif m["stance"] == "GO" and auditor["stance"] == "NO" and m["member_id"] in ("edge_analyst", "momentum_scout"):
            out["stance"] = "HOLD"
            out["rationale"] = f"{m['rationale']} Revised: calibration quality is too weak to size any edge."
        revised.append(out)
    return revised


def _build_verdict(revised: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    go = sum(1 for m in revised if m["stance"] == "GO")
    no = sum(1 for m in revised if m["stance"] == "NO")
    abstain = sum(1 for m in revised if m["stance"] == "ABSTAIN")
    eligible = len(revised) - abstain

    if facts["approved_count"] == 0:
        verdict = "HOLD"
        confidence = 0.0
        rationale = "Fail-closed: no Risk-Gate-approved opportunity exists in this domain."
    elif no > 0 or go == 0 or go * 2 <= eligible:
        verdict = "HOLD"
        confidence = round(go / max(1, eligible), 4)
        rationale = "No working majority for GO; standing down until conditions improve."
    else:
        verdict = "GO"
        confidence = round(go / max(1, eligible), 4)
        rationale = (
            f"{go}/{eligible} voting members back GO with {facts['approved_count']} approved "
            "opportunities. Advisory only — the Risk Gate remains the sole execution authority."
        )
    return {
        "verdict": verdict,
        "confidence": confidence,
        "go_votes": go,
        "no_votes": no,
        "abstain_votes": abstain,
        "rationale": rationale,
    }


def convene_council(store: DuckDBStore, domain: str, topic: str | None = None) -> dict[str, Any]:
    """Run a full three-round council session and persist it."""
    definition = get_council_definition(domain)
    d = definition["domain"]
    facts = gather_domain_facts(store, d)

    openings = [_member_opening(m, facts) for m in COUNCIL_MEMBERS]
    revised = _cross_examine(openings, facts)
    verdict = _build_verdict(revised, facts)

    dissent = "; ".join(
        f"{m['name']}: {m['stance']} — {m['rationale']}"
        for m in revised
        if verdict["verdict"] == "GO" and m["stance"] != "GO"
    ) or None

    session = {
        "session_id": f"CS-{d[:3]}-{utc_now_iso().replace(':', '').replace('-', '').replace('+', 'Z').replace('.', '')[:17]}",
        "domain": d,
        "topic": (topic or "").strip()[:200] or None,
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "rounds": [
            {"round": "OPENING", "stances": openings},
            {"round": "CROSS_EXAMINATION", "stances": revised},
        ],
        "member_stances": revised,
        "verdict_detail": verdict,
        "dissent": dissent,
        "key_metrics": {
            "opportunities_total": facts["opportunities_total"],
            "approved_count": facts["approved_count"],
            "best_net_edge_pct": facts["best_net_edge_pct"],
            "stale_count": facts["stale_count"],
            "pairs_registered": facts["pairs_registered"],
            "pairs_verified": facts["pairs_verified"],
            "kill_switch_active": facts["kill_switch_active"],
            "avg_brier": facts["avg_brier"],
            "resolved_prediction_samples": facts["resolved_prediction_samples"],
        },
        "convened_at": utc_now_iso(),
        "advisory_only": True,
        "live_trading_enabled": False,
    }
    store.save_council_session(session)
    return session
