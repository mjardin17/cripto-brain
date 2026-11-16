"""Fluctuation analysis: what moves the domain, and when the next move is due.

Two questions, answered from the app's own recorded history:

1. WHAT is making the market fluctuate? — Every recorded event reaction names
   its cause (event_type). Ranking causes by total recorded impact gives a
   deterministic attribution of volatility drivers.

2. WHEN is the next fluctuation? — For each cause with enough history, the
   intervals between occurrences have a median and a consistency. The next
   occurrence is forecast at last occurrence + median interval, with a
   probability taken from the fraction of historical intervals that were
   shorter than the requested horizon.

Fail-closed rules:
- Fewer than 3 analogs for a cause means no timing forecast ("insufficient
  history"), not a guess.
- Everything is advisory only; nothing here authorizes trades.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.intelligence.council import COUNCIL_DOMAINS
from backend.storage.duckdb_store import DuckDBStore

MIN_ANALOGS_FOR_TIMING = 3
MAX_FORECAST_PROBABILITY = 0.95
MAX_FORECAST_CONFIDENCE = 0.85
DEFAULT_HORIZON_DAYS = 30.0


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _days_between(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86_400.0


def analyze_fluctuation_drivers(store: DuckDBStore, *, domain: str) -> dict[str, Any]:
    """Attribute recorded fluctuations to their causes, ranked by total impact."""
    d = domain.strip().upper()
    if d not in COUNCIL_DOMAINS:
        raise ValueError(f"Unknown domain '{domain}'. Valid: {COUNCIL_DOMAINS}")
    reactions = store.list_event_reactions(domain=d, limit=300)

    if not reactions:
        return {
            "domain": d,
            "recorded_reactions": 0,
            "drivers": [],
            "reason": "No recorded event reactions yet — record how past events moved this domain to attribute fluctuations.",
        }

    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in reactions:
        by_type.setdefault(str(r.get("event_type") or "UNKNOWN"), []).append(r)

    drivers: list[dict[str, Any]] = []
    for et, items in by_type.items():
        magnitudes = [abs(float(i["reaction_magnitude_pct"])) for i in items if i.get("reaction_magnitude_pct") is not None]
        directions = [str(i.get("reaction_direction") or "FLAT") for i in items]
        down = sum(1 for x in directions if x == "DOWN")
        up = sum(1 for x in directions if x == "UP")
        dominant = "DOWN" if down >= up else "UP"
        median_mag = round(statistics.median(magnitudes), 2) if magnitudes else None
        total_impact = round(sum(magnitudes), 2) if magnitudes else 0.0
        base_rate = round(max(down, up) / len(items), 4)
        drivers.append(
            {
                "event_type": et,
                "analog_count": len(items),
                "dominant_direction": dominant,
                "base_rate": base_rate,
                "median_magnitude_pct": median_mag,
                "total_impact_pct": total_impact,
                "last_occurred_at": max(
                    (str(i.get("occurred_at") or "") for i in items), default=""
                ),
            }
        )

    total_recorded_impact = sum(x["total_impact_pct"] for x in drivers)
    drivers.sort(key=lambda x: x["total_impact_pct"], reverse=True)
    for x in drivers:
        x["impact_share"] = (
            round(x["total_impact_pct"] / total_recorded_impact, 4)
            if total_recorded_impact > 0
            else 0.0
        )

    top = drivers[0]
    return {
        "domain": d,
        "recorded_reactions": len(reactions),
        "top_driver": top["event_type"],
        "drivers": drivers,
        "volatility_note": (
            f"{top['event_type']} accounts for {top['impact_share']:.0%} of recorded "
            f"{d} fluctuation impact, moving it {top['dominant_direction']} "
            f"(median {top['median_magnitude_pct']}% over {top['analog_count']} recorded events)."
            if top["total_impact_pct"] > 0
            else "Recorded events carry no magnitude data — add magnitudes to attribute impact."
        ),
        "advisory_only": True,
    }


def forecast_next_fluctuations(
    store: DuckDBStore, *, domain: str, horizon_days: float = DEFAULT_HORIZON_DAYS
) -> dict[str, Any]:
    """Predict when the next fluctuation of each recorded cause is due."""
    d = domain.strip().upper()
    if d not in COUNCIL_DOMAINS:
        raise ValueError(f"Unknown domain '{domain}'. Valid: {COUNCIL_DOMAINS}")
    horizon = max(1.0, min(float(horizon_days), 365.0))
    reactions = store.list_event_reactions(domain=d, limit=300)

    if not reactions:
        return {
            "domain": d,
            "horizon_days": horizon,
            "forecasts": [],
            "reason": "No recorded event reactions yet — record analog history before forecasting timing.",
        }

    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in reactions:
        by_type.setdefault(str(r.get("event_type") or "UNKNOWN"), []).append(r)

    forecasts: list[dict[str, Any]] = []
    for et, items in by_type.items():
        dated: list[tuple[datetime, dict[str, Any]]] = []
        for i in items:
            ts = _parse_ts(str(i.get("occurred_at") or ""))
            if ts is not None:
                dated.append((ts, i))
        dated.sort(key=lambda x: x[0])

        entry: dict[str, Any] = {
            "event_type": et,
            "analog_count": len(items),
            "insufficient_history": True,
        }

        if len(dated) < MIN_ANALOGS_FOR_TIMING:
            entry["reason"] = (
                f"Only {len(dated)} dated analog(s) — at least {MIN_ANALOGS_FOR_TIMING} are needed for a timing forecast."
            )
            forecasts.append(entry)
            continue

        intervals = [_days_between(dated[k][0], dated[k + 1][0]) for k in range(len(dated) - 1)]
        intervals = [x for x in intervals if x >= 0]
        if not intervals:
            entry["reason"] = "Analog events share the same timestamp — no inter-arrival rhythm to model."
            forecasts.append(entry)
            continue

        median_interval = statistics.median(intervals)
        if median_interval <= 0:
            entry["reason"] = "Analog events share the same timestamp — no inter-arrival rhythm to model."
            forecasts.append(entry)
            continue
        stdev = statistics.pstdev(intervals) if len(intervals) > 1 else 0.0
        consistency = max(0.0, 1.0 - min(1.0, stdev / median_interval))

        last_ts, last_item = dated[-1]
        next_expected = last_ts + timedelta(days=median_interval)
        days_until = _days_between(datetime.now(timezone.utc), next_expected)

        # Empirical probability the next interval fits inside the horizon.
        within = sum(1 for x in intervals if x <= horizon)
        probability = round(min(MAX_FORECAST_PROBABILITY, within / len(intervals)), 4)

        magnitudes = [
            abs(float(i["reaction_magnitude_pct"]))
            for _, i in dated
            if i.get("reaction_magnitude_pct") is not None
        ]
        directions = [str(i.get("reaction_direction") or "FLAT") for _, i in dated]
        down = sum(1 for x in directions if x == "DOWN")
        up = sum(1 for x in directions if x == "UP")
        dominant = "DOWN" if down >= up else "UP"

        sample_factor = min(1.0, len(dated) / (MIN_ANALOGS_FOR_TIMING + 3))
        confidence = round(
            min(
                MAX_FORECAST_CONFIDENCE,
                (0.3 + 0.5 * consistency) * (0.6 + 0.4 * sample_factor),
            ),
            4,
        )

        entry.update(
            {
                "insufficient_history": False,
                "median_interval_days": round(median_interval, 2),
                "interval_consistency": round(consistency, 4),
                "last_occurred_at": last_ts.isoformat(),
                "next_expected_at": next_expected.isoformat(),
                "days_until_expected": round(days_until, 2),
                "expected_direction": dominant,
                "expected_magnitude_pct": (
                    round(statistics.median(magnitudes), 2) if magnitudes else None
                ),
                "direction_base_rate": round(max(down, up) / len(dated), 4),
                "probability_within_horizon": probability,
                "horizon_days": horizon,
                "confidence": confidence,
            }
        )
        forecasts.append(entry)

    forecasts.sort(
        key=lambda x: (
            x.get("insufficient_history", True),
            -(x.get("probability_within_horizon") or 0.0),
        )
    )
    return {
        "domain": d,
        "horizon_days": horizon,
        "forecasts": forecasts,
        "advisory_only": True,
        "live_trading_enabled": False,
    }
