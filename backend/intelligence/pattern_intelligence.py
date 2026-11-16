"""Event-pattern intelligence: historical event reactions feed better predictions.

The system records how markets reacted to past exogenous events (natural
disasters, geopolitical shocks, exchange outages, ...). When a similar event
recurs — or is expected — the recorded analogs are analyzed to produce a
deterministic, advisory prediction:

- Base rate: fraction of prior analogs that moved the domain in the same
  direction as the median reaction.
- Expected move: median magnitude of the analog set.
- Confidence: grows with sample size and base rate, hard-capped at 0.90.

Design rules (fail-closed, like the rest of the stack):
- Every prediction must name its analog set; zero analogs means no prediction.
- Predictions are recorded in the standard predictions table with
  market_category "event_pattern", so resolution feeds the existing Brier
  calibration loop and makes future pattern calls measurably better.
- Advisory only: pattern intelligence never authorizes trades.
"""

from __future__ import annotations

import statistics
import uuid
from typing import Any

from backend.intelligence.council import COUNCIL_DOMAINS
from backend.storage.duckdb_store import DuckDBStore, utc_now_iso

MIN_SAMPLE_FOR_FULL_CONFIDENCE = 5
MAX_PREDICTION_PROBABILITY = 0.95
MAX_PATTERN_CONFIDENCE = 0.90


def record_event_reaction(
    store: DuckDBStore,
    *,
    domain: str,
    event_type: str,
    event_label: str,
    occurred_at: str,
    reaction_direction: str,
    narrative: str,
    asset_label: str | None = None,
    market_label: str | None = None,
    reaction_magnitude_pct: float | None = None,
    sources: list[str] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Persist one historical event reaction for later analog matching."""
    d = domain.strip().upper()
    if d not in COUNCIL_DOMAINS:
        raise ValueError(f"Unknown domain '{domain}'. Valid: {COUNCIL_DOMAINS}")
    et = event_type.strip().upper()
    if len(et) < 2:
        raise ValueError("event_type must be at least 2 characters")
    direction = reaction_direction.strip().upper()
    if direction not in ("UP", "DOWN", "FLAT"):
        raise ValueError("reaction_direction must be UP, DOWN or FLAT")
    mag = None
    if reaction_magnitude_pct is not None:
        mag = float(reaction_magnitude_pct)
        if not (-500.0 <= mag <= 500.0):
            raise ValueError("reaction_magnitude_pct must be within [-500, 500]")
    if len(narrative.strip()) < 10:
        raise ValueError("narrative must be at least 10 characters")

    reaction = {
        "reaction_id": f"EVR-{uuid.uuid4().hex[:12].upper()}",
        "domain": d,
        "event_type": et,
        "event_label": event_label.strip()[:160],
        "occurred_at": occurred_at.strip(),
        "asset_label": (asset_label or "").strip()[:80] or None,
        "market_label": (market_label or "").strip()[:160] or None,
        "reaction_direction": direction,
        "reaction_magnitude_pct": mag,
        "narrative": narrative.strip()[:2000],
        "sources": [str(s)[:300] for s in (sources or [])][:10],
        "keywords": [str(k)[:60].lower() for k in (keywords or [])][:20],
        "created_at": utc_now_iso(),
    }
    store.save_event_reaction(reaction)
    return reaction


def analyze_event_pattern(
    store: DuckDBStore, *, domain: str, event_type: str
) -> dict[str, Any]:
    """Analyze the analog set for (domain, event_type) and build a prediction template."""
    d = domain.strip().upper()
    et = event_type.strip().upper()
    analogs = store.list_event_reactions(domain=d, event_type=et, limit=100)

    if not analogs:
        return {
            "domain": d,
            "event_type": et,
            "analog_count": 0,
            "predictable": False,
            "reason": "No recorded analog events for this domain and event type — record reactions first.",
        }

    directions = [a["reaction_direction"] for a in analogs]
    magnitudes = [a["reaction_magnitude_pct"] for a in analogs if a.get("reaction_magnitude_pct") is not None]
    up = sum(1 for x in directions if x == "UP")
    down = sum(1 for x in directions if x == "DOWN")
    flat = sum(1 for x in directions if x == "FLAT")
    dominant = "DOWN" if down >= up else ("UP" if up > down else "FLAT")
    median_mag = round(statistics.median(magnitudes), 2) if magnitudes else None
    base_rate = round((max(up, down) / len(analogs)) if len(analogs) else 0.0, 4)

    n = len(analogs)
    sample_factor = min(1.0, n / MIN_SAMPLE_FOR_FULL_CONFIDENCE)
    probability = round(min(MAX_PREDICTION_PROBABILITY, 0.5 + base_rate / 2.0), 4) if dominant != "FLAT" else 0.5
    confidence = round(
        min(MAX_PATTERN_CONFIDENCE, (0.35 + 0.5 * base_rate) * (0.6 + 0.4 * sample_factor)), 4
    )

    return {
        "domain": d,
        "event_type": et,
        "analog_count": n,
        "predictable": True,
        "dominant_direction": dominant,
        "base_rate": base_rate,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "median_magnitude_pct": median_mag,
        "analog_probability": probability,
        "confidence": confidence,
        "insufficient_sample": n < 3,
        "analogs": [
            {
                "reaction_id": a["reaction_id"],
                "event_label": a["event_label"],
                "occurred_at": a["occurred_at"],
                "direction": a["reaction_direction"],
                "magnitude_pct": a.get("reaction_magnitude_pct"),
                "narrative": a["narrative"],
            }
            for a in analogs[:10]
        ],
        "reason": (
            f"Across {n} recorded analog(s), the dominant reaction was {dominant}"
            + (
                f" with a median magnitude of {median_mag}%"
                if median_mag is not None
                else ""
            )
            + f"; base rate {base_rate:.0%}."
        ),
    }


def predict_reaction_to_event(
    store: DuckDBStore,
    *,
    domain: str,
    event_type: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Advisory prediction for how the domain will react to an upcoming event.

    The analog base rate becomes the recorded prediction probability, so the
    existing Brier calibration loop scores pattern calls over time.
    """
    analysis = analyze_event_pattern(store, domain=domain, event_type=event_type)
    out: dict[str, Any] = dict(analysis)
    out["advisory_only"] = True
    out["live_trading_enabled"] = False
    out["description"] = (description or "").strip()[:500] or None

    if not analysis.get("predictable"):
        return out

    dominant = analysis["dominant_direction"]
    if dominant == "FLAT":
        out["prediction"] = {
            "direction": "FLAT",
            "probability": 0.5,
            "confidence": analysis["confidence"],
            "note": "Analog set has no dominant direction — treating as a coin flip.",
        }
        return out

    out["prediction"] = {
        "direction": dominant,
        "probability": analysis["analog_probability"],
        "expected_magnitude_pct": analysis["median_magnitude_pct"],
        "confidence": analysis["confidence"],
        "analog_count": analysis["analog_count"],
        "note": (
            "Advisory pattern call from recorded analog events; record the actual "
            "outcome after the event so calibration scoring tightens the next call."
        ),
    }
    return out
